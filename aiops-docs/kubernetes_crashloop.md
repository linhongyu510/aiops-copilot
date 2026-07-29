# Kubernetes Pod CrashLoopBackOff 处理方案

## 文档元数据

- 场景：Pod 反复重启、CrashLoopBackOff、启动探针失败
- 常见告警：KubePodCrashLooping、ContainerRestart
- 适用对象：Kubernetes Deployment、StatefulSet、Job
- 风险级别：P1/P2
- 变更原则：先保留退出原因和上一次容器日志，再决定回滚或重启

## 现象与关键区别

`CrashLoopBackOff` 表示容器启动后反复退出并进入退避，不是根因。必须区分：

- 应用进程主动退出；
- OOMKilled；
- liveness/startup probe 配置错误；
- ConfigMap、Secret 或挂载缺失；
- 镜像或启动命令错误；
- 下游依赖不可用。

## 五分钟证据清单

1. namespace、Pod、容器、工作负载、镜像版本。
2. Pod 状态、restartCount、lastState、exitCode、reason。
3. 当前容器日志和 `--previous` 上一次容器日志。
4. Pod Events、探针失败、调度和挂载事件。
5. 最近 Deployment、ConfigMap、Secret 和镜像变更。

## 排查步骤

### 步骤一：检查退出原因

```bash
kubectl -n <namespace> get pod <pod> -o wide
kubectl -n <namespace> describe pod <pod>
kubectl -n <namespace> get pod <pod> -o jsonpath="{.status.containerStatuses[*]}"
```

关注 `OOMKilled`、exitCode、signal 和 terminated message。查询命令失败时不能推断 Pod 正常。

### 步骤二：保留上一次日志

```bash
kubectl -n <namespace> logs <pod> -c <container> --previous --tail=500
kubectl -n <namespace> logs <pod> -c <container> --tail=500
```

当前日志可能只包含新一轮启动内容，`--previous` 通常更接近退出原因。

### 步骤三：检查探针和配置

- startupProbe 是否给足初始化时间；
- livenessProbe 是否错误依赖外部服务；
- readinessProbe 是否只控制流量接入；
- Secret/ConfigMap key 是否存在；
- volume、serviceAccount 和权限是否正确；
- command、args、workingDir 是否匹配镜像。

### 步骤四：关联发布

检查 Deployment revision、镜像 digest、配置版本和发布时间。不要仅凭“发布后发生”就认定发布是根因，应结合退出日志和配置差异。

## 根因判定

| 证据 | 根因方向 | 处理 |
|---|---|---|
| reason=OOMKilled | 内存上限或泄漏 | 分析峰值和堆内存，审批后调整 |
| startupProbe 失败但进程仍初始化 | 探针预算不足 | 调整 failureThreshold/periodSeconds |
| 配置 key 缺失 | 配置发布错误 | 修复配置并滚动发布 |
| 新镜像启动命令报错 | 镜像回归 | 回滚到已验证版本 |
| 依赖连接失败后应用退出 | 启动依赖过强 | 恢复依赖或增加启动容错 |

## 止损与安全边界

低风险动作包括暂停继续发布、隔离异常版本、保留日志和扩容健康旧版本。回滚 Deployment、修改探针、调整资源限制和删除 Pod 均属于变更操作，需要权限、审计和回滚方案。

反复执行 `kubectl delete pod` 只会制造新一轮重启，不能解决确定性启动错误。

## 恢复验证

- restartCount 在观察窗口内不再增长；
- readiness 持续为 Ready；
- 新旧实例错误率和延迟恢复；
- Events 无新的探针或挂载失败；
- 发布版本、配置版本和镜像 digest 已记录；
- 补充复盘和防复发测试。

