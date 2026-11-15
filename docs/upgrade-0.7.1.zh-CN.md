# 已发布 Runtime 0.7.1 的升级指南

本页说明已发布的 Runtime 0.7.1 wheel。[main 分支指南](upgrade.zh-CN.md)还记录了
一个尚未发布的本地 Ledger 写入水位，该护栏不属于 0.7.1 wheel。

Runtime 0.7.1 会保留中断升级的恢复记录及备份引用，也会在 Shared Owner 仍持有
已落盘、但关联事件尚未到达终态的回执时阻止策略 apply。它会校验配置、集合、
destination、点数与 embedding 身份，但**不会**记录本地 Ledger 写入水位。

## 保持停写窗口不中断

从创建备份到 `upgrade resume` 完成，必须一直停掉所有会写该 profile 的 Hermes
gateway、provider worker 与 MCP server。远端 destination 还要停掉其他 Owner、
设备与可写同一集合的凭据。

若任何 writer 曾恢复写入，或无法确认停写窗口全程未破，不要运行
`upgrade resume`。即使点数与 embedding fingerprint 看起来没变、`upgrade status`
显示 plan 有效，也不能直接续跑。Runtime 0.7.1 本身无法识别原地 payload 更新。

重新停掉所有 writer，然后运行：

```sh
qdrant-memory --home /path/to/profile upgrade plan --replace
```

替换会先归档旧 plan 及其备份引用，再保存新 plan。请在新的停写点重新取备份，
再用 `upgrade apply --plan <new_plan_id>` 继续。不要把旧备份当作新检查点，
也不要修改或删除旧备份。

只读的 `upgrade status` 不会改变 destination。resume 成功也不能证明每条 payload
都未改变。Cloud 或 Server destination 要在新的停写边界通过服务商重新取备份，
并继续遵守既有隔离与 operator backup 确认要求。
