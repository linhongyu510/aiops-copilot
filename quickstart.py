#!/usr/bin/env python3
"""One-command launcher for AIOps Copilot.

Goal: ``python quickstart.py`` gets a newcomer to a working page, whatever is
installed. Nothing here is required to *use* the project - it only removes the
manual steps (venv, .env, Milvus, MCP servers, indexing) that used to live in a
README checklist.

Design rules:
- Never fail because an optional dependency is missing. Detect, report, degrade.
- Never overwrite an existing ``.env``.
- Print exactly what was started and what was skipped, so a demo is never
  accidentally presented as "full stack" when it is running degraded.

Modes:
  auto   (default) detect what is available and start the best tier
  demo   offline CLI diagnosis only, no LLM key and no Docker needed
  full   API + MCP servers + 本地 Wiki 检索（默认无需 Docker；可选 Milvus 向量后端）
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
LOG_DIR = RUNTIME_DIR / "logs"
PID_DIR = RUNTIME_DIR / "pids"
DEFAULT_PORT = 9900
IS_WINDOWS = platform.system() == "Windows"

MCP_SERVERS = (
    ("cls", "mcp_servers/cls_server.py", 8003),
    ("monitor", "mcp_servers/monitor_server.py", 8004),
    ("ops", "mcp_servers/ops_server.py", 8005),
)


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #

_COLOR = sys.stdout.isatty() and not IS_WINDOWS


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def step(text: str) -> None:
    print(_paint(f"\n▸ {text}", "1;36"), flush=True)


def ok(text: str) -> None:
    print(f"  {_paint('✓', '32')} {text}", flush=True)


def warn(text: str) -> None:
    print(f"  {_paint('!', '33')} {text}", flush=True)


def fail(text: str) -> None:
    print(f"  {_paint('✗', '31')} {text}", flush=True)


def info(text: str) -> None:
    print(f"    {text}", flush=True)


# --------------------------------------------------------------------------- #
# environment probing
# --------------------------------------------------------------------------- #


def venv_python() -> Path:
    """Return the interpreter to run services with."""
    candidate = (
        PROJECT_ROOT
        / ".venv"
        / ("Scripts" if IS_WINDOWS else "bin")
        / ("python.exe" if IS_WINDOWS else "python")
    )
    return candidate if candidate.exists() else Path(sys.executable)


def has_module(python: Path, module: str) -> bool:
    result = subprocess.run(
        [str(python), "-c", f"import {module}"],
        capture_output=True,
        cwd=PROJECT_ROOT,
    )
    return result.returncode == 0


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.6)
        return probe.connect_ex((host, port)) == 0


def read_env_value(key: str) -> str:
    """Read one key from .env without requiring python-dotenv."""
    if not ENV_FILE.exists():
        return ""
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return ""


# --------------------------------------------------------------------------- #
# preparation
# --------------------------------------------------------------------------- #


def ensure_env_file() -> None:
    step("检查配置文件 .env")
    if ENV_FILE.exists():
        ok(".env 已存在，保持不变（不会覆盖你已填写的密钥）")
        return
    if not ENV_EXAMPLE.exists():
        warn("缺少 .env.example，跳过；服务将使用内置默认值")
        return
    ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    ok("已从 .env.example 生成 .env")
    info("如需接入真实模型，请在 .env 填写 LLM_API_KEY")


def ensure_directories() -> None:
    for directory in (RUNTIME_DIR, LOG_DIR, PID_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def has_llm_key() -> bool:
    return bool(read_env_value("LLM_API_KEY") or os.environ.get("LLM_API_KEY"))


# --------------------------------------------------------------------------- #
# demo tier: offline CLI, always works
# --------------------------------------------------------------------------- #


MIN_PYTHON = (3, 11)
# ``aiops_core`` itself needs only pydantic + loguru on top of the stdlib.
CORE_REQUIREMENTS = ("pydantic", "loguru")


def python_version_of(python: Path) -> tuple[int, int] | None:
    """Return the (major, minor) version of an interpreter, or None if unknown."""
    result = subprocess.run(
        [str(python), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        major, minor = result.stdout.split()[:2]
        return int(major), int(minor)
    except (ValueError, IndexError):
        return None


def missing_core_requirements(python: Path) -> list[str]:
    return [name for name in CORE_REQUIREMENTS if not has_module(python, name)]


def install_core_requirements(python: Path) -> bool:
    """Install the two small packages the offline core needs.

    Tries pip first, then `uv pip` — uv-created virtualenvs have no pip inside.
    """
    info("正在安装离线内核依赖（pydantic、loguru）…")
    packages = ["pydantic>=2.12,<2.13", "loguru>=0.7.2"]
    attempts: list[list[str]] = [
        [str(python), "-m", "pip", "install", "--quiet", *packages],
    ]
    if shutil.which("uv"):
        attempts.append(["uv", "pip", "install", "--quiet", "--python", str(python), *packages])

    last_output = ""
    for command in attempts:
        result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True)
        if result.returncode == 0:
            ok("离线内核依赖安装完成")
            return True
        last_output = (result.stderr or result.stdout or "").strip()

    warn("自动安装失败")
    tail = last_output.splitlines()
    if tail:
        info(tail[-1])
    return False


def run_demo(python: Path) -> int:
    step("离线诊断演示（无需密钥、无需 Docker）")

    version = python_version_of(python)
    if version is not None and version < MIN_PYTHON:
        fail(
            f"当前解释器为 Python {version[0]}.{version[1]}，"
            f"本项目要求 {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+"
        )
        info("请用较新的解释器运行，例如：python3.11 quickstart.py --mode demo")
        info("或先创建虚拟环境：uv sync --extra dev")
        return 1

    missing = missing_core_requirements(python)
    if missing:
        warn(f"缺少离线内核依赖：{', '.join(missing)}")
        if not install_core_requirements(python):
            info(f"请手动安装后重试：{python} -m pip install pydantic loguru")
            return 1

    alert = PROJECT_ROOT / "tests" / "fixtures" / "kafka_lag_alert.json"
    if not alert.exists():
        fail(f"缺少示例告警文件：{alert}")
        return 1

    ok(f"输入告警：{alert.relative_to(PROJECT_ROOT)}")
    print()
    result = subprocess.run(
        [str(python), "-m", "aiops_core.cli", "oncall", str(alert)],
        cwd=PROJECT_ROOT,
        env={**os.environ, "LOGURU_LEVEL": "WARNING"},
    )
    print()
    if result.returncode == 0:
        ok("离线诊断完成：Skill 匹配 → 计划编排 → 报告渲染，全程确定性、可复现")
        info("这条链路不依赖 LLM，可在无网络环境下复现同一份报告")
    else:
        fail("离线诊断失败，请查看上方输出")
    return result.returncode


# --------------------------------------------------------------------------- #
# infrastructure
# --------------------------------------------------------------------------- #


def start_milvus() -> bool:
    step("启动向量数据库 Milvus")
    if port_in_use(19530):
        ok("Milvus 已在 19530 端口运行")
        return True
    if not docker_available():
        warn("Docker 不可用，跳过 Milvus（将使用本地 Wiki 检索后端）")
        info("影响：使用本地 Wiki 检索后端（SQLite FTS5/BM25），无需向量数据库")
        return False

    compose_file = PROJECT_ROOT / "vector-database.yml"
    if not compose_file.exists():
        warn(f"缺少 {compose_file.name}，跳过 Milvus")
        return False

    info("正在拉起容器（首次运行需要下载镜像，可能较慢）…")
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        warn("Milvus 启动失败，将以无向量检索模式继续")
        info((result.stderr or "").strip().splitlines()[-1] if result.stderr else "")
        return False

    for _ in range(60):
        if port_in_use(19530):
            ok("Milvus 就绪（localhost:19530）")
            return True
        time.sleep(1)
    warn("Milvus 启动超时，将以无向量检索模式继续")
    return False


def spawn(name: str, args: list[str]) -> subprocess.Popen | None:
    """Start a background service, redirecting output into .runtime/logs."""
    log_path = LOG_DIR / f"{name}.log"
    handle = log_path.open("a", encoding="utf-8")
    creation = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    try:
        process = subprocess.Popen(
            args,
            cwd=PROJECT_ROOT,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=creation,
            start_new_session=not IS_WINDOWS,
        )
    except OSError as exc:
        fail(f"{name} 启动失败：{exc}")
        return None
    (PID_DIR / f"{name}.pid").write_text(str(process.pid), encoding="utf-8")
    return process


def start_mcp_servers(python: Path) -> list[str]:
    step("启动 MCP 工具服务")
    started: list[str] = []
    for name, script, port in MCP_SERVERS:
        if port_in_use(port):
            ok(f"{name} 已在 {port} 端口运行")
            started.append(name)
            continue
        if not (PROJECT_ROOT / script).exists():
            warn(f"{name}: 缺少 {script}，跳过")
            continue
        if spawn(f"mcp_{name}", [str(python), script]) is not None:
            ok(f"{name} 已启动（端口 {port}，日志 .runtime/logs/mcp_{name}.log）")
            started.append(name)
    return started


def wait_for_api(port: int, timeout: int = 90) -> bool:
    """Poll /live until the API answers or the timeout expires."""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/live"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(1)
    return False


def start_api(python: Path, port: int) -> bool:
    step("启动 FastAPI 服务")
    if port_in_use(port):
        # Reusing whatever already holds the port silently serves stale code
        # after an edit, so say plainly what is being reused and how to restart.
        ok(f"端口 {port} 已有服务在运行，复用它")
        info(f"若刚修改过代码，先执行 python {Path(__file__).name} --stop 再重新启动")
        return True
    if spawn("api", [str(python), "-m", "app.run"]) is None:
        return False
    info("等待服务就绪…")
    if wait_for_api(port):
        ok("API 就绪（日志 .runtime/logs/api.log）")
        return True
    fail("API 启动超时，请查看 .runtime/logs/api.log")
    return False


def index_documents(port: int) -> None:
    """Index the bundled runbooks so retrieval has content to search."""
    step("索引运维知识库")
    query = urllib.parse.urlencode({"directory_path": "aiops-docs"})
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/index_directory?{query}",
        data=b"",
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
        data = body.get("data", {})
        indexed = data.get("indexed_files", data.get("total_files", "?"))
        chunks = data.get("total_chunks")
        detail = f"文档数：{indexed}" + (f"，切片数：{chunks}" if chunks else "")
        ok(f"知识库索引完成（{detail}）")
    except urllib.error.HTTPError as exc:
        warn(f"索引接口返回 {exc.code}，可稍后在页面上传 Runbook")
    except Exception as exc:  # noqa: BLE001 - indexing is best-effort
        warn(f"索引跳过：{type(exc).__name__}")
        info("索引失败，可稍后在页面上传 Runbook；当前检索后端可能尚未就绪")


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #


def print_summary(
    port: int,
    milvus: bool,
    mcp: list[str],
    llm: bool,
    retrieval_backend: str = "local_wiki",
) -> None:
    line = "─" * 58
    print(_paint(f"\n{line}", "36"))
    print(_paint("  AIOps Copilot 已启动", "1;32"))
    print(_paint(line, "36"))
    print(f"\n  控制台     http://127.0.0.1:{port}")
    print(f"  接口文档   http://127.0.0.1:{port}/docs")
    print(f"  健康检查   http://127.0.0.1:{port}/health")

    print(_paint("\n  当前能力", "1"))
    print(
        f"    {'✓' if llm else '—'} LLM 对话与诊断"
        f"{'' if llm else '（未配置 LLM_API_KEY，将走确定性降级路径）'}"
    )
    if retrieval_backend == "local_wiki":
        print("    ✓ 本地 Wiki 检索（SQLite FTS5/BM25，42 篇内置 Runbook）")
    elif retrieval_backend == "milvus" and milvus:
        print("    ✓ 向量检索 RAG（Milvus · BGE）")
    else:
        print("    — 向量检索 RAG（Milvus 未运行）")
    print(f"    {'✓' if mcp else '—'} MCP 工具服务{'（' + ', '.join(mcp) + '）' if mcp else ''}")

    if not llm:
        print(_paint("\n  提示", "33"))
        print("    在 .env 填写 LLM_API_KEY 后重新运行本脚本即可开启完整问答")

    print(_paint("\n  停止服务", "1"))
    print("    python quickstart.py --stop")
    print(_paint(f"{line}\n", "36"))


def process_alive(pid: int) -> bool:
    """Return True while the process still exists."""
    if IS_WINDOWS:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(name: str, pid: int) -> bool:
    """Stop one service, escalating to SIGKILL when it ignores SIGTERM.

    A plain SIGTERM is not enough on its own: uvicorn's reloader and worker can
    keep the port bound, which made `--stop` report success while the old build
    kept serving requests. So the exit is verified rather than assumed.
    """
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        info(f"{name} 已不在运行")
        return False
    except PermissionError:
        warn(f"{name}（pid {pid}）无权限停止，请手动处理")
        return False

    for _ in range(20):  # up to ~5s of graceful shutdown
        if not process_alive(pid):
            ok(f"已停止 {name}（pid {pid}）")
            return True
        time.sleep(0.25)

    if not IS_WINDOWS:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        for _ in range(12):
            if not process_alive(pid):
                ok(f"已强制停止 {name}（pid {pid}）")
                return True
            time.sleep(0.25)

    warn(f"{name}（pid {pid}）仍在运行，请手动检查")
    return False


def stop_services() -> int:
    step("停止后台服务")
    pid_files = sorted(PID_DIR.glob("*.pid")) if PID_DIR.exists() else []
    if not pid_files:
        ok("没有由本脚本启动的服务")
        return 0

    stopped = 0
    for pid_file in pid_files:
        name = pid_file.stem
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid_file.unlink(missing_ok=True)
            continue
        if terminate(name, pid):
            stopped += 1
        pid_file.unlink(missing_ok=True)

    if stopped == 0:
        ok("没有正在运行的后台服务")
    info("Milvus 容器如需停止：docker compose -f vector-database.yml down")
    return 0


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="quickstart",
        description="AIOps Copilot 一键启动",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "demo", "full"),
        default="auto",
        help="auto=自动选择；demo=离线诊断演示；full=完整服务栈",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="API 端口")
    parser.add_argument("--stop", action="store_true", help="停止本脚本启动的服务")
    parser.add_argument("--no-index", action="store_true", help="跳过知识库索引")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    ensure_directories()
    if args.stop:
        return stop_services()

    print(_paint("\n  AIOps Copilot · 一键启动", "1;36"))
    python = venv_python()
    version = python_version_of(python)
    version_label = f"{version[0]}.{version[1]}" if version else "未知"
    info(f"解释器：{python}（Python {version_label}）")

    ensure_env_file()

    step("检查运行环境")
    server_ready = has_module(python, "fastapi") and has_module(python, "uvicorn")
    ok("FastAPI 依赖已安装" if server_ready else "未检测到 FastAPI 依赖")

    mode = args.mode
    if mode == "auto":
        mode = "full" if server_ready else "demo"
        info(f"自动选择模式：{mode}")

    if mode == "demo":
        if not server_ready:
            info("如需完整服务栈：uv sync --extra full  或  pip install -e '.[full]'")
        return run_demo(python)

    if not server_ready:
        fail("full 模式需要服务端依赖")
        info("请先执行：uv sync --extra full  或  pip install -e '.[full]'")
        info("或改用离线演示：python quickstart.py --mode demo")
        return 1

    milvus = start_milvus()
    mcp = start_mcp_servers(python)
    if not start_api(python, args.port):
        return 1

    retrieval_backend = read_env_value("AIOPS_RETRIEVAL_BACKEND") or "local_wiki"
    if not args.no_index:
        info(f"检索后端：{retrieval_backend}")
        index_documents(args.port)

    llm = has_llm_key()
    print_summary(args.port, milvus, mcp, llm, retrieval_backend=retrieval_backend)

    if not args.no_browser:
        try:
            import webbrowser

            webbrowser.open(f"http://127.0.0.1:{args.port}")
        except Exception:  # noqa: BLE001 - opening a browser is optional
            pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        raise SystemExit(130) from None
