# Kubernetes Deployment 发布失败处理方案

## 识别信号

Deployment updatedReplicas 或 availableReplicas 长期低于期望值，Progressing=False 或出现 ProgressDeadlineExceeded。

## 排查顺序

1. 查看 rollout status、ReplicaSet 与新 Pod Event。
2. 检查镜像拉取、启动命令、配置挂载、探针和资源 request。
3. 对比新旧 ReplicaSet 的镜像、环境配置和 selector。
4. 检查 maxUnavailable/maxSurge 是否与容量约束冲突。
5. 结合错误率和延迟判断是否已经影响真实流量。

## 止损与恢复

确认新版本导致故障时执行受控回滚，保留失败 Pod 和日志证据。恢复标准是上一稳定版本副本就绪、错误率回落且无继续扩散。

## 升级条件

数据库迁移不可逆、配置同时影响新旧版本或回滚后仍失败时立即升级发布负责人。
