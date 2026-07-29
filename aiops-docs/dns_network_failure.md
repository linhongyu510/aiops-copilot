# DNS 与网络连接失败处理方案

## 文档元数据

- 场景：域名解析失败、连接超时、连接重置、跨服务网络异常
- 常见告警：DNSResolutionFailure、ConnectionTimeout、PacketLossHigh
- 适用对象：主机、容器、Kubernetes、HTTP/TCP 服务
- 风险级别：P1/P2
- 变更原则：从客户端、DNS、网络路径和服务端四层取证，禁止用持续重试掩盖故障

## 现象与分类

- `NXDOMAIN`：域名不存在或搜索域错误；
- `SERVFAIL`：DNS 服务端处理失败；
- 解析超时：DNS 网络或服务不可用；
- `connection refused`：目标可达但端口未监听或被主动拒绝；
- `connection timed out`：网络路径、防火墙或服务无响应；
- `connection reset`：连接被对端或中间设备重置。

不同错误不能统一归类为“网络不通”。

## 五分钟证据清单

1. 源服务、目标域名/IP/端口、环境和时间窗口。
2. DNS 查询结果、响应码、解析耗时和 DNS Server。
3. TCP 连接结果、超时阶段和失败比例。
4. 同节点、跨节点、同可用区和跨可用区对比。
5. 目标服务健康、监听端口和最近网络策略变更。

## 排查步骤

### 步骤一：验证解析

```bash
nslookup <host>
dig <host>
getent hosts <host>
```

记录查询使用的 DNS Server 和返回码。单次成功不能排除间歇性故障，应结合错误率和时间序列。

### 步骤二：验证 TCP 路径

```bash
curl -v --connect-timeout 3 https://<host>/health
nc -vz <host> <port>
```

避免在生产环境进行高频扫描。HTTP 5xx 表示连接已建立，问题通常不在基础 TCP 连通性。

### 步骤三：检查 Kubernetes 网络

- Service 是否有 EndpointSlice；
- Pod 是否 Ready；
- NetworkPolicy 是否允许源到目标；
- CoreDNS 是否健康、是否出现超时；
- 节点 conntrack、CNI 和跨节点网络是否异常。

### 步骤四：关联变更

检查 DNS 记录、证书、Ingress、Service selector、NetworkPolicy、防火墙和路由变更。没有审计记录时应标记证据缺失。

## 止损措施

- 降低非核心调用频率，启用有界退避和熔断；
- 将流量切换到已验证的健康实例或区域；
- 对解析故障使用经过审批且有过期时间的临时记录；
- 恢复错误的 DNS/Service/NetworkPolicy 变更。

禁止把 IP 长期硬编码进应用，也不要无限增加重试次数；这会放大依赖故障。

## 恢复验证

- DNS 成功率和解析延迟恢复；
- TCP/HTTP 连接成功率恢复；
- 应用超时、重置和 5xx 回到基线；
- 多节点、多实例验证一致；
- 临时绕行配置已经登记撤销时间；
- 保留变更和网络证据。

