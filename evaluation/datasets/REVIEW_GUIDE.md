# 评测数据复核与真实告警导入边界

## 当前数据能怎么称呼

- `rag_queries.jsonl`：规则生成的可复现查询集，未完成人工复核时不能称为人工标注集。
- `incident_cases.jsonl`：合成故障夹具，不能称为真实生产事故。
- `anonymized_alerts.jsonl`：只有在授权来源提供预脱敏记录，并通过导入检查后才会生成；导入后仍为 `pending`，需要具名复核。

## 导入授权且预脱敏的真实告警

1. 复制 `anonymized_alerts_template.csv` 到仓库外，填写服务别名，不填写真实主机名、IP、邮箱、手机号、工号、密钥或客户标识。
2. 确认数据所有者允许将脱敏摘要用于个人项目评测。
3. 执行：

   ```powershell
   uv run python evaluation/import_anonymized_alerts.py D:\secure\alerts.csv `
     --acknowledge-authorized-source
   ```

4. 导入器会拒绝常见邮箱、IPv4、手机号与凭据模式，但自动规则不能替代人工脱敏检查。
5. 复核人填写 `reviewer`、`review_notes` 并把 `review_status` 改为 `approved` 后，才可表述为“经人工复核”。

## 每次投递前固定检查

```powershell
uv run python evaluation/review_labels.py validate
uv run python evaluation/review_labels.py precheck
uv run python evaluation/incident_review.py validate
uv run python evaluation/review_status.py
```

以 `artifacts/reliability/human_review_status.json` 为最终表述边界，不凭记忆修改简历数字。
