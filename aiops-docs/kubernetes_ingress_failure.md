# Kubernetes Ingress 访问失败处理方案

## 识别信号

域名返回 404、502、503、TLS 错误或连接超时。先判断请求是否到达 Ingress Controller。

## 排查顺序

1. 核对 DNS 是否指向正确入口地址。
2. 检查 IngressClass、host、path、pathType 和 controller Event。
3. 验证 Service selector、port/targetPort 与 EndpointSlice。
4. 检查后端 Pod readiness 和 NetworkPolicy。
5. TLS 场景检查 Secret 引用、证书域名和有效期。

## 止损与恢复

新规则引发故障时回滚上一版本；后端不可用时按预案切换静态降级页或备用服务。恢复后从集群内外分别验证 DNS、TLS 和业务路径。

## 升级条件

入口控制器副本异常、多个域名同时失败或云负载均衡不可用时升级平台与网络团队。
