# Kubernetes Pod OOMKilled 处理方案

## 识别信号

Pod 重启次数增长，容器上次退出原因为 `OOMKilled`，退出码通常为 137。先区分容器达到自身 memory limit，还是节点发生全局内存压力。

## 排查顺序

1. 查看 Pod status、restartCount、limit/request 和最近 Event。
2. 对齐重启前后的 working set、RSS、堆内存和请求量曲线。
3. 检查同节点是否存在 MemoryPressure、系统 OOM 或其他容器突增。
4. 比较当前版本与上一版本的内存基线，定位缓存、批任务、大对象或泄漏。

## 止损与恢复

流量高峰优先限流或降低并发；确认 limit 明显低于稳定工作集时再临时调高。不要只靠无限增加 limit 掩盖泄漏。恢复后观察至少两个业务高峰，并验证重启次数不再增长。

## 升级条件

节点持续 MemoryPressure、多个工作负载同时 OOM，或调高 limit 后内存仍单调增长时，升级平台与应用负责人联合处理。
