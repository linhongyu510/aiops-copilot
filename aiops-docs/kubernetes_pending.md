# Kubernetes Pod Pending 处理方案

## 识别信号

Pod 长时间停留在 Pending，尚未绑定节点。核心证据来自 Scheduler Event，而不是容器日志。

## 排查顺序

1. 读取 Event 中的 `FailedScheduling` 原因。
2. 核对 CPU/内存 request、节点可分配资源和 Pod 数量上限。
3. 检查 nodeSelector、affinity、topology spread、taint/toleration 是否互相矛盾。
4. 检查 PVC 是否 Pending、StorageClass 是否可用。
5. 若使用配额，核对 ResourceQuota 与 LimitRange。

## 止损与恢复

错误约束应回滚到上一可用配置；容量不足时优先扩容或降低非关键任务 request，不要直接删除系统 Pod 腾空间。恢复标准是 Pod 成功调度且副本数达到期望值。

## 升级条件

所有节点均资源不足、存储控制器异常或调度规则涉及多团队共享策略时升级集群管理员。
