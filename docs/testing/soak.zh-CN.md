# Soak 与故障注入测试

本页说明分阶段 soak 系统：在 GitHub 托管的 Runner 上执行真实的 Owner、MCP
Gateway、Ledger 与 Qdrant 链路。它存在的原因是一次 24 小时云端 soak 发现了
客户端既未确认、也未重放的重启边界写入（[记录](../validation/cross-device-cloud-soak-2026-10.md)）。
修复是重试契约（[设计](../design/remote-write-retry-adr.md)），而本 Harness 是
保证该契约不回退的回归门禁。

分阶段是刻意设计：先跑短窗口，由独立 Gate 依据证据判定，通过后才允许启动长测。
构建有问题时，代价是几分钟，而不是数十 Runner 小时。

## 拓扑

只有 Owner 与 Harness 是原生进程，因此 `SIGTERM`、`SIGKILL`、重启与 Ledger
恢复都是真实行为。

```text
GitHub-hosted Runner
├── 原生 Python
│   ├── Qdrant Memory Owner（remote-serve，loopback HTTP + Shared Service）
│   ├── soak 客户端（产品 remote MCP 客户端 + durable outbox）
│   └── 确定性 embedding / extraction provider（loopback）
└── Docker
    ├── Qdrant 服务器（固定 digest、API Key）
    └── toxiproxy（仅 network 场景，固定 digest）

TLS
  soak 客户端 --HTTPS--> nginx --HTTP--> Owner（127.0.0.1）
  Owner --HTTPS--> nginx --HTTP--> Qdrant（Docker）
```

客户端经与部署文档一致的 HTTPS 代理策略访问 Owner：固定 `Host`、拒绝转发头、
限制请求体、短 header 超时。两个 TLS 监听都使用本次 Run 的私有 CA，不会信任
共享证书。

## 模式

| 模式 | 阶段 | 每条腿窗口 |
| --- | --- | --- |
| `quick` | preflight、smoke 矩阵、Gate、最终报告 | 480 秒 |
| `full` | quick 全部阶段 + 长测矩阵 | 18000 秒（5 小时） |

`full` 先跑 smoke 矩阵；只有 smoke Gate 通过、且（如启用）Cloud 窗口通过后，
长测矩阵才会启动。

触发方式为 `workflow_dispatch` 与每周定时（`0 3 * * 0`，在 `main` 上跑 `full`）。
没有 `push` 或 `pull_request` 触发，因此不可信变更无法触及任何 Cloud 凭证。

## 六条腿

两种架构都跑全部场景：

| 场景 | 覆盖 | 故障注入 |
| --- | --- | --- |
| `steady` | add、search、精确去重、update、delete、CAS 竞争、capture 事件、私有/项目隔离、数据量增长 | 无 |
| `restart` | `SIGTERM`、`SIGKILL`、在途请求、回执重放、恢复与 WAL、租约重新获取 | quick 每 45 秒、full 每 120 秒重启一次，每三次一次强制 |
| `network` | 连接重置、读超时、延迟、短时断连、丢响应、`Retry-After` | toxiproxy 作用于 Owner → Qdrant 与 客户端 → Owner，另有主动丢弃响应 |

每条腿使用七个预配置凭证：三个项目写者、两个只读凭证、两个私有分区。每个设备
持有独立 token，Owner 只保存 token 哈希。私有分区按逻辑身份成对共享，从而在
应当隔离处验证隔离、在应当共享处验证共享。

## 硬性不变量

校验器把已确认的 journal 与 Ledger 行、以及 Qdrant 精确点逐项对比。检索命中
从不作为写入成功的证据。

| 不变量 | 含义 |
| --- | --- |
| `write_durability` | 每条已确认写入以其确认版本存在；已删除的记录不会复活 |
| `request_id_idempotency` | 每个 request 标识只有一条回执、一条记录 |
| `event_id_idempotency` | 每个生命周期事件只有一条回执、一条记忆 |
| `authorization` | 只读凭证不能写入 |
| `scope_isolation` | 私有记录不会泄露给其他凭证 |
| `ledger_integrity` | `integrity_check` 通过，无 PENDING/FAILED/未完成回执 |
| `no_unexplained_loss` | 没有确认写入丢失，也没有未解释的失败 |
| `writer_lease` | 一个窗口内只有一个 Owner 进程，每次重启都成功就绪 |
| `harness_clean` | Harness 自身没有未捕获异常 |
| `window_complete` | 实测窗口不少于目标的 95% |
| `retrieval_quality` | 检索质量单独度量，不与写入丢失混为一谈 |
| `workload_volume` | 窗口真的产生了已确认的工作量 |
| `probe_results` | 每个注入探针都给出了经过验证的结果 |
| `scenario_coverage` | 场景确实注入了它所声称测试的故障 |

结果只能是 `PASS`、`FAIL`、`INCOMPLETE` 或 `HARNESS_ERROR`。`skipped`、
`cancelled`、缺失 Artifact、缺失证据都不会被翻译成 `PASS`。

## 证据

每条腿先写持久 JSONL，再写其他任何东西，因此崩溃无法删除已产生的窗口。

| 文件 | 内容 |
| --- | --- |
| `events.jsonl` | 每次尝试与每个逻辑操作一条，另含控制流与故障边界 |
| `report.json` | 身份、状态、不变量结果、计数、延迟、Ledger 状态、趋势 |
| `metrics.json` | 窗口内的资源与存储采样 |
| `junit.xml` | 每个不变量一条用例，Runner 据此记录结果 |
| `owner.log` | 脱敏后的 Owner 日志（截断到最近 8 MiB） |
| `ledger/` | `state.db` 与其 sidecar 的只读副本 |

journal 中的记忆文本由 Harness 为本次 Run 生成，不含用户内容或生产记忆。Artifact
不含 token、API Key 或超出只读副本的 Ledger 数据。脱敏器会移除已登记
凭证与显式的 `Bearer`/`api_key=` 形式；提交哈希与内容哈希保持可读，因为无法
核对的证据不是证据。

## GitHub Environment 配置

两个 Environment 承载全部配置。不要复用 `qdrant-cloud`、`model-quality` 或
`model-smoke`。

### `soak-local`

| 设置 | 值 |
| --- | --- |
| 分支 | 仅 `main` |
| Secrets | 无 |
| Variables | 无 |
| 审批人 | 无 |
| Workflow 用法 | `environment: {name: soak-local, deployment: false}` |

此处 `deployment: false` 是安全的：该 Environment 没有依赖 deployment 对象的
保护规则，Job 只需要 Environment 名称。

### `soak-cloud`

| 必需性 | 名称 | 类型 |
| --- | --- | --- |
| 必需 | `SOAK_QDRANT_URL` | variable |
| 必需 | `SOAK_QDRANT_COLLECTION` | variable |
| 必需 | `SOAK_QDRANT_API_KEY` | secret |
| 可选 | `SOAK_LLM_BASE_URL` | variable |
| 可选 | `SOAK_LLM_MODEL` | variable |
| 可选 | `SOAK_LLM_API_KEY` | secret |

分支：仅 `main`。保留 Required reviewer。Workflow **不**对该 Environment 设置
`deployment: false`，因为人工审批必须挂在 deployment 对象上；Cloud Job 会先等待
人工批准，这是预期行为。

Workflow 强制执行的规则：

- 仅当 `include_cloud` 为真、smoke Gate 通过、ref 为 `refs/heads/main` 且仓库为
  上游仓库时才运行 Cloud 窗口。
- 启用了 `include_cloud` 但缺少**必需**项时，Job 失败并列出缺失名称与设置页路径，
  绝不静默跳过。
- 可选的 LLM 名称用于以真实模型验证生命周期捕获；缺失时 Cloud 窗口用确定性本地
  provider 覆盖工具面，并在报告中说明。
- Secret 值只在 Cloud Job 的步骤级 `env` 中读取；其他 Job 不引用任何
  `vars.SOAK_*` 或 `secrets.SOAK_*`。

### 创建 Environment

维护者在仓库设置页或通过 API 一次性创建两个 Environment。名称固定，Workflow 不会
自动创建。

```bash
gh api -X PUT repos/<owner>/<repo>/environments/soak-local \
  -F 'deployment_branch_policy[protected_branches]=false' \
  -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X PUT repos/<owner>/<repo>/environments/soak-cloud \
  -F 'reviewers[][type]=User' -F 'reviewers[][id]=<user-id>' \
  -F 'deployment_branch_policy[protected_branches]=false' \
  -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X POST repos/<owner>/<repo>/environments/soak-local/deployment-branch-policies \
  -f name=main
gh api -X POST repos/<owner>/<repo>/environments/soak-cloud/deployment-branch-policies \
  -f name=main
gh variable set SOAK_QDRANT_URL --env soak-cloud --body 'https://<cluster>'
gh variable set SOAK_QDRANT_COLLECTION --env soak-cloud --body '<collection>'
gh variable set SOAK_LLM_BASE_URL --env soak-cloud --body 'https://<llm-endpoint>'
gh variable set SOAK_LLM_MODEL --env soak-cloud --body '<model>'
gh secret set SOAK_QDRANT_API_KEY --env soak-cloud
gh secret set SOAK_LLM_API_KEY --env soak-cloud
```

`SOAK_LLM_BASE_URL` 和 `SOAK_LLM_MODEL` 属于非敏感配置，应通过 `gh variable set` 设置。
`SOAK_LLM_API_KEY` 等需要保密的 LLM 凭据仍应通过 `gh secret set` 设置；
命令会交互式读取值且不会回显。核对用：
`gh api repos/<owner>/<repo>/environments/<name>` 及其 `variables`、`secrets`
子资源，它们只返回名称与元数据。

### 固定 Collection 与清理

Cloud 窗口写入 `SOAK_QDRANT_COLLECTION` 指定的固定测试 Collection。每次 Run 用
由自身运行身份派生的专属 `owner_id` 标记自己的记录，清理只删这些记录：

1. 统计本次 Run 作用域内的记录数；
2. 按 filter 删除；
3. 再次统计并要求为零；
4. 要求 Collection 本身仍然存在。

Workflow 另用独立步骤重新读取 Collection，证明两件事：本次 Run 的记录已不存在，
且 Collection 本身仍在。若清理无法证明剩余为零，该 Run 标记为 `CLEANUP_FAILED`；
Collection 永不删除。

embedding 指纹包含 provider base URL，因此 Cloud Job 固定本地 provider 端口
（`18431`）。没有稳定端口时，固定 Collection 每次 Run 都会表现为另一条流水线。

## 本地运行

前置条件：Docker、带 SSL 模块的 `nginx`、`openssl`，以及已安装 `remote` extra 的
本项目。

```bash
python -m scripts.soak.run \
  --scenario steady --profile quick --target-seconds 120 --workers 3 \
  --workdir /path/outside/the/repo/work \
  --report-dir /path/outside/the/repo/report \
  --platform-label local
```

工作目录不能位于符号链接父目录之下：保护模式会拒绝该路径，这在 macOS 上很关键
（`/var`、`/tmp` 都是链接）。

本地注意事项：

- macOS：用 `brew install nginx` 安装 nginx，工作目录放在 home 下，不要用
  `$TMPDIR`。
- Docker Desktop 通过 `host.docker.internal` 访问宿主机，因此 network 场景使用
  端口映射；Linux 上 Harness 改用 host 网络，所有监听仍在 loopback。
- 每次 Run 按名称（`soak-qdrant-<seed>`）独占自己的容器，teardown 时移除。同机
  并发运行必须使用不同 seed。

## 在 GitHub 上运行

```bash
gh workflow run soak.yml --ref main -f mode=quick -f include_cloud=false
gh workflow run soak.yml --ref main -f mode=full  -f include_cloud=false
gh workflow run soak.yml --ref main -f mode=quick -f include_cloud=true -f cloud_seconds=180
```

`workflow_dispatch` 要求 Workflow 文件已存在于默认分支，而两个 Environment 都只
允许 `main`。因此从特性分支触发会被设计性阻断：先合并，再触发。

成本量级：`quick` 使用六台 Runner，各约 12 分钟；`full` 使用六台 Runner，各最长
350 分钟，约 35 Runner 小时。

## 阅读结果

最终 Job 会按阶段打印表格，并且只要存在非 `PASS` 的预期腿就失败。常见现象：

| 现象 | 含义 |
| --- | --- |
| `INCOMPLETE` + `missing artifact` | 该腿被跳过、取消，或在写证据前退出 |
| `INCOMPLETE` + `window ... cannot support` | 窗口短于验收要求 |
| `HARNESS_ERROR` | Harness 失败，产品状态未知，绝不算通过 |
| `CLEANUP_FAILED` | 本次 Run 的 Cloud 记录未被证明清除 |
| `unresolved` 大于零 | 恢复排空后仍有写入从未获得确认 |
| `duplicate_records` 大于零 | 重放产生了第二条逻辑记录 |
| `pending` 或 `quarantined` 大于零 | Ledger 仍有需要人工处置的工作 |

`network` 腿出现较长的 p99 是预期行为：客户端会遵守 `Retry-After`。
`latency_clean_p50_p95_p99` 排除了需要重放的操作，因此限流停顿不会掩盖常规路径。
`expected_path_outages` 统计发生在已声明客户端故障窗口内、请求尚未到达 Owner 的
握手重置。

## 边界

- 六条并行五小时腿不等于单个 Owner 连续运行 24 小时；记录会写明实测对象。
- embedding 与提取是确定性本地 provider：本 Harness 度量持久性、隔离与重启行为，
  不度量模型质量。
- 单一 Owner 语义不变。Harness 不会为了模拟另一台设备而对同一 Collection 启动
  第二个 Owner。
- 长测矩阵是回归门禁，不是容量基准；吞吐数字只描述托管 Runner 上的一个 loopback
  部署。
