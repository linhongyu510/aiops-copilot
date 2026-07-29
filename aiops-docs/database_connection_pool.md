# 数据库连接池耗尽告警处理方案

## 文档元数据

- 场景：数据库连接池耗尽、获取连接超时、连接数持续升高
- 常见告警：ConnectionPoolExhausted、TooManyConnections、ConnectionTimeout
- 适用对象：Java/HikariCP、Python SQLAlchemy、MySQL
- 风险级别：P1/P2
- 变更原则：先取证、再限流；禁止未经审批直接扩大数据库最大连接数

## 现象与影响

应用可能出现 HTTP 500/503、请求排队、数据库连接获取超时。数据库侧可能同时出现 `Threads_connected` 接近 `max_connections`、活跃事务增加或慢查询堆积。连接池耗尽是结果，不等于数据库连接上限过小；常见根因包括慢 SQL、事务未提交、连接泄漏、依赖抖动和流量突增。

## 五分钟证据清单

1. 明确服务名、实例、环境、告警开始时间和影响接口。
2. 查询连接池 active、idle、pending、timeout 指标及其变化趋势。
3. 查询数据库当前连接数、运行线程、慢查询、锁等待和长事务。
4. 查询应用日志中的连接获取超时、事务超时和连接泄漏告警。
5. 对齐发布、配置变更、流量和下游数据库故障时间。

必须区分：

- “查询失败”不代表连接数为零；
- “连接数高”不代表存在连接泄漏；
- “池已满”不代表可以直接增大池容量。

## 排查步骤

### 步骤一：确认影响范围

- 单实例异常：优先检查实例连接泄漏、线程池或局部流量倾斜。
- 全部实例异常：优先检查数据库容量、慢 SQL、锁等待和公共依赖。
- 单接口异常：按 trace_id 关联 SQL、事务范围和外部调用。

### 步骤二：检查应用连接池

关注：

- active 是否长期等于 maximum；
- pending 是否持续增加；
- timeout 是否在告警窗口突增；
- idle 是否始终为零；
- 连接使用时长是否明显增加。

若 active 高但数据库活跃 SQL 很少，检查连接泄漏、长事务或线程阻塞。若数据库运行 SQL 同步增多，继续检查慢查询和容量。

### 步骤三：检查 MySQL

只读查询示例：

```sql
SHOW GLOBAL STATUS LIKE 'Threads_connected';
SHOW GLOBAL STATUS LIKE 'Threads_running';
SHOW VARIABLES LIKE 'max_connections';
SELECT * FROM information_schema.innodb_trx
ORDER BY trx_started ASC LIMIT 20;
```

生产查询必须使用只读账号并限制返回行数。不要在诊断工具中执行 `KILL`、修改参数或提交事务。

### 步骤四：关联日志和变更

搜索关键词：

- `connection timeout`
- `pool exhausted`
- `too many connections`
- `leak detection`
- `lock wait timeout`
- `communications link failure`

重点关联最近发布是否扩大事务范围、遗漏连接释放、引入批量查询或降低超时。

## 根因判定

| 证据组合 | 优先假设 | 进一步验证 |
|---|---|---|
| active 满、pending 增长、慢 SQL 增多 | 慢 SQL 占用连接 | 查看执行计划和调用接口 |
| active 满、数据库活跃 SQL 少 | 连接泄漏或长事务 | 检查连接使用时长和事务 |
| 多服务同时连接失败 | 数据库故障或容量耗尽 | 检查数据库健康与连接上限 |
| 发布后单实例异常 | 代码或配置回归 | 对比版本和连接池配置 |
| 流量与连接数同步增长 | 容量不足或缺少限流 | 核对容量模型和入口流量 |

## 止损措施

### 低风险

- 对非核心接口限流或降级；
- 暂停高并发批处理和非必要定时任务；
- 缩短异常外部依赖的等待时间；
- 将流量从异常实例摘除。

### 需要审批

- 回滚最近发布；
- 重启存在明确连接泄漏的单个实例；
- 终止长事务；
- 调整连接池或数据库连接上限。

扩大应用连接池可能把故障从应用转移到数据库，必须核对数据库总连接预算。

## 恢复验证

- pending 和 timeout 恢复到基线；
- active 不再持续触顶；
- HTTP 5xx 与 P95/P99 延迟恢复；
- 慢查询和锁等待下降；
- 至少观察两个告警窗口；
- 记录根因、变更、证据链接和后续负责人。

