# Elasticsearch Cluster Red 处理方案

## 识别信号

集群 health 为 red，至少一个 primary shard 未分配，相关索引可能无法读写。

## 排查顺序

1. 列出 unassigned primary shard 与 allocation explanation。
2. 检查节点离线、磁盘水位、分片过滤和版本兼容。
3. 查看 master 日志、集群状态大小和 pending tasks。
4. 判断是否有可用副本或快照，确认数据恢复路径。
5. 对齐节点重启、索引创建和磁盘变更。

## 止损与恢复

停止非关键写入和大查询，优先恢复原节点或释放已确认的安全空间。强制分配 stale primary 可能丢数据，必须审批。恢复后 health 至少为 yellow，并验证关键索引。

## 升级条件

无有效副本、快照不可用或需要强制分配时立即升级搜索平台负责人。
