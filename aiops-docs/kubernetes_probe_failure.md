# Kubernetes 健康探针失败处理方案

## 识别信号

Event 出现 liveness/readiness/startup probe failed。Readiness 失败会摘除流量；Liveness 失败会重启容器，两者影响不同。

## 排查顺序

1. 记录失败探针类型、HTTP 状态、超时和连续失败次数。
2. 在 Pod 网络内验证 path、port、scheme 与 Host header。
3. 对齐应用启动耗时、GC、线程池、下游超时和 CPU throttling。
4. 检查探针是否依赖非关键外部系统，导致级联摘流或重启。

## 止损与恢复

启动慢应调整 startupProbe，不要盲目放宽 liveness。下游短暂异常时，readiness 可反映服务能力，但 liveness 应只判断进程是否不可恢复。配置变更先灰度一个副本。

## 升级条件

探针通过但真实流量失败，或多个服务同时因同一下游探针失败时升级应用架构负责人。
