# Kubernetes NetworkPolicy 连接中断处理方案

## 识别信号

Pod DNS 正常但 TCP 连接超时，且故障与 NetworkPolicy 发布或命名空间标签变更时间一致。

## 排查顺序

1. 明确源 Pod、目标 Pod、协议和端口。
2. 列出双方 namespace/pod label 与生效的 ingress/egress policy。
3. 检查默认拒绝策略、DNS egress 和 ipBlock 例外。
4. 验证 CNI 是否支持所用规则并检查 CNI 日志。
5. 从同标签测试 Pod 复现，避免用节点网络得出错误结论。

## 止损与恢复

只添加最小范围的临时放行，不要删除整个默认拒绝策略。修复 selector 后通过允许与拒绝用例双向验证。

## 升级条件

策略计算正确但数据面仍丢包，或 CNI 多节点异常时升级网络平台团队。
