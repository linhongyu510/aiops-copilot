# Kafka Consumer Rebalance 风暴处理方案

## 识别信号

Consumer Group 状态在 PreparingRebalance/CompletingRebalance 间反复切换，吞吐下降并伴随重复处理。

## 排查顺序

1. 查看成员加入退出、generation 和 rebalance 原因。
2. 检查 session.timeout、heartbeat、max.poll.interval 与单批处理耗时。
3. 排查消费者进程重启、探针过严和网络抖动。
4. 检查订阅 topic 分区变更及实例频繁扩缩容。
5. 判断是否支持 cooperative sticky assignor。

## 止损与恢复

暂停自动扩缩容震荡，降低单批处理时间或临时减少批量；参数调整需成组验证。恢复后 group 状态应稳定，lag 开始收敛。

## 升级条件

协调器异常、多个 Group 同时震荡或 Broker 控制面不稳定时升级消息平台团队。
