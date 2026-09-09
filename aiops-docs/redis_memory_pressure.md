# Redis 内存压力处理方案

## 识别信号

used_memory 接近 maxmemory，evicted_keys 增长，延迟升高或写入返回 OOM。需区分数据增长、内存碎片和客户端缓冲区。

## 排查顺序

1. 查看 INFO memory、keyspace、stats 与 maxmemory-policy。
2. 比较 used_memory、used_memory_rss 和 mem_fragmentation_ratio。
3. 检查 key 数、过期比例、大 Key 分布和客户端输出缓冲。
4. 对齐业务流量、缓存 TTL 或版本变更。
5. 集群模式检查槽位和节点内存是否不均。

## 止损与恢复

优先限制异常写入、恢复合理 TTL 或扩容。删除 Key 属于数据变更，必须确认业务语义；禁止用全库扫描阻塞实例。恢复后确认 evicted_keys 不再异常增长。

## 升级条件

持久化失败、主从同时高内存或淘汰策略可能造成关键数据丢失时升级缓存负责人。
