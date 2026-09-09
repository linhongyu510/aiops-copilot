# 贡献指南

欢迎 issue 和 PR。

## 本地环境

```bash
git clone https://github.com/linhongyu510/aiops-copilot.git
cd aiops-copilot
uv sync --extra dev --extra local-embeddings   # 或 pip install -e '.[full,dev]'
python quickstart.py --mode demo               # 零依赖验证环境可用
```

## 提交前必须通过

```bash
pytest tests -q --no-cov
ruff check app aiops_core mcp_servers evaluation tests
node --check static/app.js
```

可选安装 pre-commit 自动执行：

```bash
pre-commit install
```

## 代码约定

- 行宽 100，遵循 `pyproject.toml` 中的 ruff / black 配置。
- 新增工具必须在注册中心声明 `read_only`、`risk_level` 和所需角色。**任何变更类工具（`risk_level > 0`）都必须经 ActionGovernor 提案与人工审批**，不得绕过。
- 外部依赖（Milvus / Redis / Prometheus / K8s 等）缺失时应**明确返回不可用**，不要伪造数据或静默回退到演示数据。
- 修改检索链路时，必须保留 `trace.degradations` 的降级语义：单分支失败不得影响其他分支的证据。

## 测试要求

- 新功能需要覆盖正常路径与失败降级路径。
- 涉及外部服务的测试用 `@pytest.mark.integration` 标记，并在缺少环境时 skip 而非 fail。
- 前端改动需保证 `tests/test_frontend_shell.py` 通过；调整静态资源后同步递增 `index.html` 中的 `?v=` 版本号（CSS 与 JS 必须一致）。

## 提交信息

使用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
feat: 新增 Redis 慢日志诊断工具
fix: 修复 hidden 属性被组件 display 覆盖
perf: dense 与 BM25 召回并发执行
docs: 补充 MCP 挂载说明
test: 覆盖 embedding 缓存淘汰逻辑
```

## 不要提交

- `.env`、任何真实密钥或凭据
- `.runtime/`、`logs/`、`volumes/`、`artifacts/` 等运行时与生成产物
- 未脱敏的真实告警、日志或业务数据
