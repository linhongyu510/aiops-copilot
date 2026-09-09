# Kubernetes HPA 扩缩容异常处理方案

## 识别信号

HPA 当前副本长期不变、指标显示 unknown、出现 FailedGetResourceMetric 或 AbleToScale=False。

## 排查顺序

1. 查看 HPA conditions、current/desired replicas 与目标值。
2. 验证 Metrics API 或自定义指标适配器是否返回数据。
3. 确认 Pod 配置了资源 request；资源利用率型 HPA 依赖 request。
4. 检查 min/max、behavior、稳定窗口和扩缩容速率限制。
5. 排除发布中 selector 不匹配或指标标签漂移。

## 止损与恢复

容量风险高时可按审批临时固定安全副本数，同时修复指标链路。不要在指标失真时频繁手动扩缩。恢复后用受控负载验证 desired replicas 能随指标变化。

## 升级条件

Metrics API 全局异常或自定义指标定义涉及业务口径变更时升级平台与业务负责人。
