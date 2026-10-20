# Universal Owner 分批交付与验收报告

记录日期：2026-10-20。正式需求仍以用户提供的开发任务书为准。
本报告记录实际提交、推送、PR 和验证结果；六个 PR 均为 Draft，尚未合并。
任务书整体尚未全部验收完成，不能据此发布正式版本。

## 本批实际交付

| 仓库 / PR | 审查基线 | 内容 | 功能提交 |
|---|---|---|---|
| Runtime [#66](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/66) | main | 默认关闭的 Native 共享召回、会话隔离、有界等待、完整 JSON 与来源信息 | `dee66804fc489938ef7f3d32330425de0657f641` |
| Runtime [#67](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/67) | #66 分支 | 逻辑 Agent / 设备授权、撤销轮换、持久事件计划、恢复与安全取消 | `9ee0e6e01adc7be1c35907e58b0b65f85cd40915` |
| Runtime [#68](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/68) | #67 分支 | 原生 MCP Streamable HTTP、HTTPS 客户端、认证及资源限制、真实网络 CI | `e04f859e7a5a8cbd780d007c5a32e4fd69d30a3b` |
| Runtime [#69](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/69) | #68 分支 | Hermes Native Owner、自动生命周期、故障门禁、Cloud 验收通道、运维及研发报告 | `b01bb5a5aa86f68b70133be4fe574122b3429760` |
| MCP [#32](https://github.com/cnkang/qdrant-memory-mcp/pull/32) | main | Pi 自动生命周期与持久 outbox、真实 Hermes → Pi 验收、其他 Harness 预览配置 | `0c9e6dafe07d9e45171b85f2287271c69a8ed1fa` |
| Skill [#29](https://github.com/cnkang/skills/pull/29) | main | 授权、部署、事件控制及已发布 / 源码预览能力边界 | `fc1209b7c02e5fc027d2da90d27033da22907310` |

Runtime 按 #66 → #67 → #68 → #69 审查。上表是功能提交；后续报告提交不改变执行代码。
MCP 的预览 CI 固定引用 Runtime `b01bb5a...`，不再引用修正前的开发分支。
审查修正使用追加提交和逐层 merge commit 同步，没有 force push 或重写已推送历史。
没有合并 PR、发布版本、安装线上服务、使用生产凭据或进行破坏性迁移。

原始 Runtime 工作目录的 the integration branch 保留为开发历史快照，含一个本地报告提交；
该报告已进入 #69。后续研发应以 #69 的完整功能分支为准，不能把旧快照当成最新实现。
四个独立工作树保留供审查。
没有清理或覆盖原始仓库的本地工作。

## 分阶段状态

| 阶段 | 状态 | 实际情况 |
|---|---|---|
| Phase 0 | PASS，后续设计边界仍需审查 | 已审查三个仓库、ADR、主分支、Release 与未合并 PR，识别已有能力后开发 |
| Phase 1 | 源码 PASS，PR 门禁待完成 | 本机跨 Agent 共享一个 Owner；默认私有召回保持；共享召回新鲜授权且不落入私有记忆 |
| Phase 2 | 源码 PASS，PR 门禁待完成 | Hermes Native 与本机 / 远程入口共用 Runtime、Ledger、租约、LLM 与恢复门禁 |
| Phase 3 | PARTIAL | HTTPS Streamable HTTP、逻辑身份、独立设备凭据及隔离已实现；OAuth/OIDC 入网仍未实现 |
| Phase 4 | PARTIAL | Hermes 和真实 Pi SDK 自动生命周期已验证；其他 Harness 目前是明确标注的桥接配置，不冒充自动生命周期 |
| Phase 5 | PARTIAL | 真实网络、跨设备身份、发行矩阵、实际跨 Harness CI 已有 PASS；新 PR 门禁、真实 Cloud 及完整多轮模型评估尚未全部完成 |

## 设计与安全决策

- 所有写入仍进入唯一 Memory Owner 和既有 Runtime；没有第二套 Runtime，也没有绕过 Owner 写 Qdrant。
- 同一逻辑 Agent 可使用多个独立设备凭据访问已授权私有分区。不同逻辑 Agent 保持隔离；授权不可在设备或 principal 之间随意重绑定。
- 每次请求重新校验授权；撤销、过期、轮换、共享写入 capture rule、事件控制都遵守相同授权边界。项目读取许可不等于允许捕获私有对话。
- Gateway 使用官方 MCP SDK，应用监听本机回环地址，由同主机 TLS 代理提供 HTTPS；客户端验证证书，不绕过 TLS、不跟随重定向、不依赖 SSH/VPN 或 Qdrant Cloud。
- Host / Origin / Forwarded / 查询参数、请求体大小、超时、并发与 principal 配额均有限制。网络错误保留受控机器错误码，不回传远端正文或凭据。
- 取消只适用于尚未生成候选或操作计划的原始 PENDING/FAILED 事件；CAS 检查状态与内容后原子清理。已准备、已提交或结果不确定的写入必须恢复；取消不能解除恢复门禁。
- Native 缓存在可能发生写入前失效，含响应丢失场景；跨作者切换不会交付之前的私有缓存。
- Embedded、Self-hosted Qdrant、Qdrant Cloud 三种后端均保留。Cloud 验收要求预创建隔离集合和受限测试凭据，不提供管理员密钥回退。

## 测试与 CI 证据

以下分别记录代码层次与具体提交，不能用历史 PASS 代替新 PR 的最终门禁。

| 检查 | 结果与边界 |
|---|---|
| #66 独立工作树全量 | 621 PASS / 7 SKIPPED / 1 已知 Embedded payload-index warning；发生于最后一轮 review 修正前 |
| #66 最后 review 修正后相关回归 | 58 PASS / 0 SKIPPED；覆盖统一配置校验、Provider、作用域与既有共享契约 |
| #67 新增测试 | 25 PASS / 0 SKIPPED；身份、撤销轮换、生命周期、取消 CAS、Host context、LLM |
| #67 原有兼容测试 | 62 PASS / 0 SKIPPED；Runtime、dedupe、SharedOwner、共享契约 |
| #68 MCP SDK 1.x / 2.x | 各 26 PASS / 0 SKIPPED，包含生产客户端与 ASGI Gateway 往返 |
| #68 两个隔离容器 / 网络实例 | SDK 1.30 实际 HTTPS 验收 PASS；包括 CRUD、TLS 信任、鉴权、Origin、同 Agent 跨设备、私有隔离、并发 CAS、丢失响应重试、重启恢复 |
| #69 Native 相关测试 | 53 PASS / 0 SKIPPED；发生于统一配置校验修正前 |
| 最后功能提交真实 Hermes → Pi | 1 PASS / 0 SKIPPED，2.46 秒；实际 Hermes MemoryManager、Native Owner、校验证书的 HTTPS、独立 Pi SDK 会话 |
| 最后功能提交 Runtime 全量覆盖率 | 681 PASS / 9 SKIPPED / 1 已知 Embedded warning；总覆盖率 90%；155.10 秒；Python 3.14.7，执行代码为 `b01bb5a...` |
| 真实 Cloud Owner | SKIPPED：未提供专用隔离测试凭据；最后功能提交实跑得到 1 SKIPPED |
| 实际 systemd 部署 | UNVERIFIED：已提供服务模板及 CLI 参数校验，没有安装或修改线上服务 |
| Ruff / 格式 / diff / workflow 语法 | 各层 staged snapshot hook PASS；最终 Runtime 122 个文件格式检查 PASS；相关 workflow actionlint PASS |

两容器验收的复现命令为 `python scripts/test_remote_mcp_docker.py 'mcp>=1.30,<2'`。
全量验证在第四层工作树运行，设置 `PYTHONPATH` 为该工作树及真实 Hermes checkout，执行
`python -m pytest -q --cov=qdrant_memory --cov-report=term:skip-covered --cov-report=xml:/tmp/qdrant-owner-split-coverage.xml`。
9 个跳过项包含可选 CPU 模型缓存、独立 TLS / JWT 与真实 Server / Cloud 参数条件；
对应远端安全通道另有实际 PASS，不能把本机 SKIPPED 混写成通过。
此前一次把 `--help` 当成参数的试运行失败于测试容器启动：该脚本首个参数实际是 SDK requirement。
使用正确参数重跑已通过；没有为此取消 TLS、认证或其他安全校验。

已完成的远端 CI：

- Runtime 功能快照 `aa9565d...` 的[真实 HTTPS / TLS/JWT / Server 设备隔离四通道](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39341532898) PASS。
- 同一快照的[平台兼容](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39341532944)、[Standalone 兼容](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39341532897)、[打包](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39341532894)、[可选本地 embedding](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39341532890) 均 PASS。
- Runtime [CI 39341532896](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39341532896) 中 Host/行为矩阵、SDK 1/2、Ruff、Snyk code、dependency 和 Sonar 均已 PASS；最后查询整条工作流仍为 queued，不能据单个 job 宣称整条工作流完成。
- MCP `cce1632...` 的[发行矩阵 12 组](https://github.com/cnkang/qdrant-memory-mcp/actions/runs/39341606084)全部 PASS；[跨 Harness CI](https://github.com/cnkang/qdrant-memory-mcp/actions/runs/39341605787)包含实际 Hermes → Pi 自动记忆任务并 PASS。
- Skill `fc1209b...` 的[Validate skills](https://github.com/cnkang/skills/actions/runs/39341654296) 与 [WorkBuddy preview](https://github.com/cnkang/skills/actions/runs/39341654286) PASS。
- 新 PR 及 MCP Runtime 引用更新后的任务仍有大量 QUEUED / IN_PROGRESS；查询时没有失败检查，也没有 inline review comment。这不等于审查完成或可以合并。

Sonar 的旧分析曾 FAIL：新代码覆盖率 72.5%，门槛 80%。补充真实生产路径测试后，
最后实时查询开发快照分支质量门禁已为 **OK / PASS，新代码覆盖率 81.7%**；
reliability / security / maintainability rating 均为 1，重复率 0%，hotspot reviewed 100%。
这是实际 Sonar 结果，独立于本地总体覆盖率 90%。仍须核验每个新 PR head 的分析与门禁，
不能用开发快照的 PASS 代替拆分后的 PR 检查。旧 FAIL 作为诊断历史保留，不再当作最新状态。

## CodeRabbit 与失败测试

本次使用 CodeRabbit CLI 对拆分后的首层做辅助 review，提出 3 issues：1 major、2 minor。
三项均核实有效并完成修正：

1. 文档仍描述不存在的 TTL / 缓存、未记录 delivery budget、错误输出形式和后台合并路径。已按实际实现修订；未知 TTL 配置也有拒绝测试。
2. 私有保留命名空间只在 Provider.initialize 拒绝，配置验证入口仍接受。先运行测试确认两个 DID NOT RAISE 失败，再把 v1/v2 前缀检查移入统一配置验证，回归转绿。
3. 新鲜召回测试依赖线程在 50ms 内完成。已使用完成的 Future 验证数据路径，保留专门的有界等待 / 超时测试，不放宽安全时限来掩盖问题。

review 修正通过 `dee6680...` 追加提交及后续逐层 merge 同步。没有宣称 CodeRabbit 全项目零问题。
更早轮次的事件取消原子性、机器错误码脱敏及 Cloud 无凭据假绿问题，已在对应阶段报告中记录修复。

## 模型与版本兼容性

实际本地 `qwen3:8b` 提取试验为 10 个匿名合成 case：precision、正例 case recall、case success 均 1.0，provider failures 为 0；p50 7206.9ms、p95 25772.3ms。
这是 pilot，不能代表完整多轮真实任务、端到端自动记忆质量或生产准确率。早期 0.857 precision / 0.8 case success 的失败仍保留在历史报告。

Runtime 0.4.0、MCP 0.2.2、Skill 0.3.1 的版本号及公开发行依赖 pin 均未升级。
新增能力属于未发布源码预览；MCP 的发行验收仍针对已发布 Runtime，源码 Native/Pi 验收独立运行并拒绝跳过。
保留 SDK 1.x / 2.x、既有六字段授权、四个 MCP memory 工具及默认私有模式。
统一拒绝 operator 私有配置中的 `shared:v1:` / `shared:v2:` 是保留分区的安全约束，不改变 Owner 根据授权生成作用域的路径。

## 未完成事项与后续 PR

1. **先关闭当前六个 Draft PR 的 review 和最终 CI 门禁。** Sonar 应对各自 head 及正确审查基线给出新结果。不得跳过安全扫描或仅因其他任务绿灯就合并。合并仍需用户明确授权。
2. **OAuth/OIDC 正式入网。** 当前是明确预配置的静态独立设备凭据 MVP；尚无发现元数据、授权码 / PKCE、发行方验证与客户端入网流程。需单独安全设计、失败测试及兼容实现。
3. **完整 L3 多轮验收和持续评估。** 扩充固定匿名数据集、多轮真实 Harness 生命周期、提取 / 检索指标及可重复 CI；10-case pilot 不能替代这项。
4. **真实隔离 Cloud Owner 验收。** 只有在提供非生产测试环境、受限凭据及专用集合后执行。缺凭据的 SKIPPED 必须保留。
5. **其他非 Hermes Harness 的自动生命周期。** 当前已满足至少一个真实 Pi 适配的实现路径；Codex、DSH、WorkBuddy、OpenCode 的自动 hook 不得根据配置示例宣称已完成，按各 Harness 实际能力分 PR 推进。
6. **部署与发布。** systemd 在专用非生产机器的真实启动 / 退出 / 恢复验收、最终兼容矩阵与版本协调应先完成；正式发布、合并及线上变更另需授权。

既有 PR 63 已由远端合并到 main，本批基于包含其脱敏修复的主分支开始；不是本次擅自合并。
依赖更新 PR 64 仍独立开放，没有混入上述功能分支或代替它做合并。

更早的实现与证据见 [完整阶段报告](universal-owner-report.zh-CN.md) 和 [后续开发报告](universal-owner-follow-up.zh-CN.md)。
