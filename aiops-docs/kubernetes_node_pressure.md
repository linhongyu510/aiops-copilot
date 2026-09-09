# Kubernetes Node Pressure 处理方案

## 识别信号

节点 Condition 出现 MemoryPressure、DiskPressure 或 PIDPressure，Pod 可能被驱逐，调度器停止向节点放置新 Pod。

## 排查顺序

1. 查看 Node Condition、Allocatable、taint 与最近驱逐 Event。
2. MemoryPressure 检查工作集、内核内存和容器 limit。
3. DiskPressure 检查 imagefs/nodefs、日志和已停止容器镜像。
4. PIDPressure 检查异常进程数、僵尸进程和进程风暴。

## 止损与恢复

先隔离节点并保护关键副本，再清理可确认的临时数据或异常工作负载。禁止无差别删除容器目录。Condition 恢复后逐步解除隔离并观察调度。

## 升级条件

压力在多个节点同时出现、驱逐影响有状态服务或根因涉及运行时/内核时升级平台团队。
