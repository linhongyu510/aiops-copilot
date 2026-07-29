# WINDOS 联动三场景记录

- 时间：2026-07-24T11:20:10.739316+00:00

## normal_live

- 成功证据：5/6
- 自动变更：False
- 端点状态：health=200、ready=200、production_readiness=200、queue=503、agent_metrics=200、governance=200

## single_endpoint_503_fault_injection

- 成功证据：4/6
- 自动变更：False
- 端点状态：health=200、ready=200、production_readiness=200、queue=503、agent_metrics=503、governance=200

## windos_unreachable_fault_injection

- 成功证据：0/6
- 自动变更：False
- 端点状态：health=502、ready=502、production_readiness=502、queue=502、agent_metrics=502、governance=502
