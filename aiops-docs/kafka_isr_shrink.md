# Kafka ISR 缩减处理方案

## 识别信号

UnderReplicatedPartitions 增长、ISR 集合缩小或副本频繁进出 ISR，可能降低故障容忍度。

## 排查顺序

1. 定位受影响 Broker、topic 和 partition。
2. 检查 Broker 网络、磁盘 IO、GC、请求队列和进程重启。
3. 比较 leader 与 follower 的 offset 差。
4. 检查机架、磁盘容量和副本分布是否集中。
5. 对齐批量写入、重分配或运维变更。

## 止损与恢复

降低非关键写入、暂停大规模重分配，优先恢复异常 Broker。不要在证据不足时频繁迁移 leader。恢复后确认 ISR 稳定且未复制分区归零。

## 升级条件

多个 Broker 同时异常、min.insync.replicas 影响写可用性或磁盘损坏时升级消息平台团队。
