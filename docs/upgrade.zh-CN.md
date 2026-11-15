# 用 upgrade 命令纳管 Qdrant 策略

Runtime 0.7.1 把过去隐含的三件事分开：

- 由 profile 配置推导的**目标**策略；
- 该 destination 已验收的**有效**策略；
- 集合的**实际**状态，包括 payload 索引。

`upgrade plan` 读取 destination 并保存一份 plan，`upgrade apply` 在 writer lease 内
执行它，`upgrade resume` 续跑中断的 plan，`upgrade status` 只读已保存的 plan。

## 开始之前

1. 停掉所有会写该 profile 的 Hermes gateway、provider worker 与 MCP server。
   远端 destination 还要停掉其他 Owner、设备与可写同一集合的凭据。
2. 确认 profile 配置、集合名与 embedding pipeline。原地升级三者都不变。
   fingerprint 不同是 blocker，不是迁移。
3. 保留当前 Runtime 版本，便于应用回退。

## Plan

```sh
qdrant-memory --home /path/to/profile upgrade plan
```

结果字段：

| 字段 | 含义 |
| --- | --- |
| `plan_id` | `apply` 与 `resume` 必须引用的标识 |
| `source_policy.status` | `missing`、`accepted`、`pending` 或 `corrupt` |
| `source_policy.effective` | 有效策略；无记录时为 `legacy` |
| `target` | 目标策略，含索引策略版本 |
| `indexes.status` | `equivalent`、`missing`、`conflict` 或 `not_applicable` |
| `indexes.entries` | 逐字段的期望与实际索引类型 |
| `indexes.extra` | 策略之外的索引，只报告并保留 |
| `checkpoint` | 记忆条数、identity sentinel 与向量 schema |
| `backend_version` | `server-info`、`embedded-engine` 或 `unavailable` |
| `blockers` | 使该 destination 不可升级的条件 |
| `warnings` | 需复核但不阻塞的条件 |

`plan` 只在 profile 内写入 plan 记录。它不写 Ledger、不建索引、不跑恢复、不改配置、
不下载模型。加 `--dry-run` 可只读取结果而不保存。

已经开始、且尚未到达 `ready` 的 plan 会保留阶段、失败原因、步骤记录和恢复点。
普通 `plan` 只报告这条记录、它的 `plan_id` 和下一条命令，不会覆盖它。请续跑该 plan。
若 plan 确实过期、无法继续，使用 `upgrade plan --replace`。替换会先归档旧 plan 及其
备份引用，再保存新 plan。不要手动删除控制面文件。

Embedded destination 需要独占本地存储。其他 writer 持有 lease 时命令以
`writer_busy` 停止，且不产生任何状态变更。

## Apply

```sh
qdrant-memory --home /path/to/profile upgrade apply --plan PLAN_ID
```

Embedded 不需要额外开关。writer lease 就是隔离边界，本地恢复点自动创建。

远端必须同时给出两个开关：

```sh
qdrant-memory --home /path/to/profile upgrade apply --plan PLAN_ID \
  --confirm-writers-isolated --accept-operator-backup
```

`--confirm-writers-isolated` 表示该 destination 的其他 writer 已全部停止。
`--accept-operator-backup` 表示你已在同一停写检查点取得服务商侧快照。
Runtime 不持有 Qdrant 管理备份权限，也不会静默跳过恢复点。

已登记受保护的 destination 还要用其他维护命令同样的 `--policy` 传入策略文件。

### 步骤

| 步骤 | 作用 |
| --- | --- |
| `backup` | Embedded：复制已关闭的存储、Ledger 与策略记录，再重新打开副本核对。远端：记录操作者声明 |
| `indexes` | 创建缺失的受管索引，然后重读 schema |
| `verify` | 重读索引、identity、点数，并用真实数据跑一次 scope 过滤 |
| `activate` | 以新 generation 写入有效策略 |

每一步幂等。步骤成功但记录未写就崩溃时，`resume` 会检测到已完成，而不是重复执行。
若 embedded 备份副本已存在、但步骤记录丢失，`resume` 会按计划的点数核验该副本并
沿用已有路径，不会创建第二个恢复点。

`apply` 与 `resume` 先取得 writer lease，再重新读取已保存的 plan。若取锁期间另一轮
替换了 plan，旧 plan 会被拒绝，新记录不会被覆盖。对已完成的 plan 重复执行只报告
`already_applied`，不会新增备份或策略 generation。

## Resume

```sh
qdrant-memory --home /path/to/profile upgrade resume --plan PLAN_ID
```

`resume` 接受与 `apply` 相同的开关。它会用当前 profile 重新校验已保存的 plan。
配置、集合、destination 或点数变化都视为过期 plan。使用 `upgrade plan --replace`
保留旧恢复记录后重新 plan，不要强行沿用。

取得 lease 后，`apply` 与 `resume` 会读取 Shared Owner 状态。已落盘、但关联事件
尚未到达终态的生命周期回执会阻止该轮操作；未完成的 shared request 同样阻止。
先完成既有 Owner 恢复流程：重启 Owner，让其生命周期恢复把已准入的计划重新入队。
若事件已终态失败，先停止 Owner，对其 profile 运行运维 `retry`，再重启。维护命令
不会自动清理回执、重新提取或重放待处理事件。恢复完成后，原 plan 仍有效则 resume；
已过期则使用 `upgrade plan --replace`。

## Status

```sh
qdrant-memory --home /path/to/profile upgrade status
```

`status` 只读控制面，不打开 destination，因此服务不可达时也能安全查看。
`unplanned` 表示没有 plan；`planned` 表示可 apply；`applying`、`verifying` 表示中途停止；
`failed` 附带脱敏的失败类型；`ready` 表示有效策略已提交。`backup_path` 报告仍被记录的
恢复点，`plan_stale` 报告已保存的 plan 是否仍匹配当前 profile；`next` 区分 apply、
resume 与 `plan --replace`。

## Blocker 对照

| 代码 | 处置 |
| --- | --- |
| `not_initialized` | 先跑正常 `init`。升级从不创建集合 |
| `fingerprint_mismatch` | 保留当前环境，导出后在新 profile 与新集合重嵌入 |
| `dimension_mismatch`、`distance_mismatch`、`vector_datatype` | 不是原地升级，改用兼容集合 |
| `identity_missing` | 集合无可信 fingerprint，不要原地纳管 |
| `index_conflict` | 核对字段类型。Runtime 从不删除或重建冲突索引 |
| `pending_operations`、`failed_events`、`quarantined_events` | 先用既有恢复路径处理 |
| `reset_intent`、`scope_delete_intent`、`shared_pending` | 先恢复或解除这些破坏性意图 |
| `shared_owner_receipt` | 重启 Owner，让其生命周期恢复把已准入的计划重新入队。事件已终态失败时，先运行运维 `retry` 再重启。不要手动清理回执 |
| `backend_unreachable` | 修好连通性与权限后重新 plan |
| `policy_corrupt` | 策略记录不可读，先人工检查再 apply |

## 回退边界

- **应用回退**支持：重装上一版 Runtime。
- **存储降级**不支持：不要用旧 Server 或旧 embedded 客户端打开新版写过的数据。
- **按时间点恢复**支持：使用恢复点，该点之后的写入会丢弃。
- **切回旧 writer** 不是回退：新写入落在旧 writer 之后，旧 writer 看不到它们。

## Lease 覆盖不到的部分

`WriterLease` 只排除同一 profile、同一 destination 的本地协作 writer。
它不阻断另一台机器上的进程、不协作的旧客户端，或用其他凭据写同一 Cloud 集合的进程。
因此远端 apply 必须显式确认隔离。
