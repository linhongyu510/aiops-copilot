# MCP Servers

| Server | Port | Tools |
|---|---:|---|
| `cls_server.py` | 8003 | `get_current_timestamp`, `get_region_code_by_name`, `get_topic_info_by_name`, `search_topic_by_service_name`, `search_log` |
| `monitor_server.py` | 8004 | `query_cpu_metrics`, `query_memory_metrics` |
| `ops_server.py` | 8005 | `mysql_list_tables`, `mysql_describe_table`, `mysql_read_query`, `web_search` |

启动：

```powershell
.\.venv\Scripts\python.exe mcp_servers\cls_server.py
.\.venv\Scripts\python.exe mcp_servers\monitor_server.py
.\.venv\Scripts\python.exe mcp_servers\ops_server.py
```

Ops Server 配置：

```dotenv
MYSQL_DSN=mysql+pymysql://oncall_reader:password@127.0.0.1:3306/observability
MYSQL_ALLOWED_SCHEMAS=observability
MYSQL_QUERY_TIMEOUT_SECONDS=10
MYSQL_MAX_ROWS=200
TAVILY_API_KEY=
WEB_SEARCH_MAX_RESULTS=5
```

安全约束：MySQL 工具只允许单条只读查询，限制行数，并要求数据库账号本身具备只读权限。应用层校验不能替代数据库权限控制。
