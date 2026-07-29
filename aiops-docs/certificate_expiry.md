# TLS 证书过期与握手失败处理方案

## 文档元数据

- 场景：证书即将过期、证书过期、TLS 握手失败
- 常见告警：CertificateExpiringSoon、TLSHandshakeFailure
- 适用对象：Ingress、API Gateway、服务端证书和客户端信任链
- 风险级别：P1/P2
- 变更原则：核对域名、证书链、有效期和部署位置，证书私钥不得进入日志或 Agent 上下文

## 现象与影响

客户端可能报告证书过期、主机名不匹配、未知 CA、证书链不完整或协议协商失败。健康检查使用 HTTP 成功不能证明 HTTPS 证书正常。

## 证据清单

- 受影响域名、端口和 SNI；
- 证书 subject、SAN、issuer、notBefore、notAfter、序列号；
- 完整证书链是否返回；
- 客户端错误码和 TLS 版本；
- 证书部署位置及最近更新记录；
- 自动续期任务状态。

只记录证书公有信息，不得采集私钥、密钥口令或完整 Secret 内容。

## 排查步骤

### 步骤一：检查线上证书

```bash
openssl s_client -connect <host>:443 -servername <host> -showcerts
openssl x509 -noout -subject -issuer -dates -serial
```

必须使用实际域名传递 SNI，直接访问 IP 可能得到其他虚拟主机的证书。

### 步骤二：判断错误类型

| 错误 | 检查方向 |
|---|---|
| certificate expired | 有效期和续期任务 |
| hostname mismatch | SAN 与访问域名 |
| unknown authority | 客户端信任库和 CA |
| unable to get local issuer | 中间证书链 |
| handshake failure | TLS 版本、密码套件和双向认证 |

### 步骤三：定位部署层

确认 TLS 在 CDN、负载均衡、Ingress、Gateway 还是应用进程终止。更新错误层级的证书不会改变线上实际返回。

### 步骤四：检查自动续期

检查证书控制器、ACME challenge、DNS 权限、Secret 更新和工作负载重载。Secret 已更新但进程未重载时，线上仍可能返回旧证书。

## 止损与变更

低风险动作包括确认备用证书、验证新证书链和准备灰度。替换证书、修改 Ingress、重载网关均需审批和回滚方案。

禁止关闭客户端证书校验作为长期修复，也不要在工单、日志或对话中粘贴私钥。

## 恢复验证

- 不同客户端和外部探针均能完成握手；
- SAN、证书链和有效期正确；
- 线上返回证书序列号与发布版本一致；
- 错误率恢复且无旧证书实例；
- 自动续期告警和到期提前量已验证；
- 记录证书负责人和下一次到期时间。

