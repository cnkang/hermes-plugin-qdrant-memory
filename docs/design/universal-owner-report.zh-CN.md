# Qdrant Memory 跨 Agent / 跨设备研发报告

> 本文保留前轮交付快照。最新 PR #63 已合并及后续 CI 失败、剩余验收项，见
> [当前未完成事项核验](universal-owner-remaining.zh-CN.md)，不再以本文的排队状态作为当前事实。

日期：2026-10-20。范围：任务书 Phase 0–5。结论：已交付跨仓库可运行源码、
分阶段提交、实际网络/宿主验收和 CI；**完整 DoD 尚未全部通过，不宣称可用于生产**。
没有发布版本、合并 PR、修改线上部署、使用生产凭据或执行数据迁移。

## 1. 现场核验与已有工作

三个工作区起始均干净，未覆盖用户未提交内容。Runtime 起始分支是
`design/phase-1b`，包含已有 SharedReader 和未合并的 Phase 1b 设计；它们被保留，
研发另开 `codex/universal-owner`，整合了最新 main 的 Phase 0 文档。

| 仓库 | 核验的 main | 发布事实 | 当前研发分支 |
|---|---|---|---|
| Runtime | 起始 `68a9ed1a89c64b19a22fbc637f050785515fc387`；结束 `80816294611c1c0b896784cc12f86e72115699c6` | PyPI/GitHub 0.4.0；硬化及本次新能力不在已发布 0.4.0 内 | `codex/universal-owner` |
| MCP | `60d0cda936dc248ba26a7a1c12ba84ea3643f6ec` | PyPI 0.2.2 已于 10-19 10:32 UTC 上传；GitHub Release 仍 Draft | `codex/owner-lifecycle-adapters` |
| Skill | `d6062f07f35a741d5058712ab5e9966b39f87e00` | Skill 0.3.1，snapshot skills-20261019.1 | `codex/universal-owner-guidance` |

开发开始时只有 [PR #63 SharedReader](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/63)
未合并，已复用它的库，未重复实现或合并。结束核验又发现
[PR #64 Python 依赖](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/64)
修改 requirements 文件，以及
[PR #65 Actions 依赖](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/65)
修改 codeql.yml。结束前再次查询发现 #65 已由其他操作于 10-19 17:20:10 UTC 合并；
核实仅更新两个 CodeQL Action SHA 后，研发分支同步了最新 main。#64 尚未合并，
依赖版本组合仍需独立验证。MCP、Skill 没有其他
未合并 PR。已推送研发分支，未把全部 Runtime 能力混入一个巨型新 PR。

## 2. 分阶段完成度

| Phase | 实际交付 | 状态与边界 |
|---|---|---|
| 0 | 最新代码/Release/PyPI/CI/未合并 PR 核验，复用 ADR，增加实施账本、威胁边界、角色修订 | PASS；未把旧设计或旧评审当作当前实现 |
| 1 | opt-in Native 共享召回；Native 本身作为单 Owner，复用原 Runtime、Ledger、WriterLease、宿主 LLM；private 与有审计规则的 project 分开保存 | PASS：确定性 Native/真实 MemoryManager 测试；不是所有实际 Host UI 验收 |
| 2 | 官方 MCP Streamable HTTP、HTTPS 代理拓扑、逐请求认证、固定 Scope、MCP-to-MCP stdio bridge、双容器网络测试 | 受控静态服务凭据 MVP PASS；OAuth/OIDC/浏览器模式未实现，公网生产部署 UNVERIFIED |
| 3 | logical agent / principal / device 独立绑定；同 Agent private；跨 Agent project；过期、实时吊销、轮换、设备来源、CAS/回执/重启恢复 | PASS：Embedded、实际 Server、隔离网络测试；物理多设备 UI UNVERIFIED |
| 4 | Owner durable turn 事件复用现有 Extractor/Runtime；Pi 官方生命周期扩展；实际 Hermes → Owner → HTTPS → Pi 新会话自动召回 | 基础 turn 生命周期 PASS；通用取消管理 API、更多宿主和真实模型质量仍有缺口 |
| 5 | 复用 shared-security / cross-harness 工作流；部署、身份、回滚、后端及宿主配置；真实模型/Embedding 小样本基线 | 部分 CI 已实跑 PASS，最新提交完整矩阵仍需完成；真实提取诊断 FAIL；Cloud SKIPPED |

## 3. 设计决策和代码变化

- 一个权威 Owner、一个 Runtime、一份 Ledger；没有第二套 Node 记忆引擎。
  Native Owner 直接复用现有 Provider 的 worker、宿主 LLM 和资源所有权。
  这项 opt-in 角色修订已写入 `universal-cross-agent-memory.md`，默认模式不变。
- `shared_recall.py` 校验只读空间和 secret 环境变量名，复用 PR #63 Reader。
  共享召回每次重新授权，不缓存晚到结果；默认交付等待 50 ms，明确配置可提高到
  250 ms，适配实际本地 Embedding 延迟。网络调用后再次检查 scope/generation，
  防止切换用户期间交付旧私有缓存。默认 private 的原路径保留。
- `native_owner.py` 使单个 Native Provider 提供本机 Owner；可选 loopback MCP
  listener 必须有 protected policy。项目自动写入需要配置列表和服务端
  `capture_rule_id` 双重 opt-in。共享授权拒绝只审计并跳过该规则，不阻断 private
  或其他设备；private 授权拒绝的原始事件保留 FAILED，显式重试只恢复 private。
- `remote_gateway.py` 通过官方 SDK 的 stateless manager 提供 `/mcp`，工具仍为四个
  固定 Scope CRUD。Host/Origin/forwarded metadata、body、并发和 principal 配额
  在边界校验；未认证流量不能消耗已认证 principal 的请求体槽位或配额。
- `remote_client.py` 使用 SDK 1.x 的 httpx / SDK 2.x 的 httpx2 做正式 MCP
  协商，保留 TLS 检查、禁止重定向和环境代理；客户端没有 Qdrant key。
- `shared_service.py` 兼容旧六字段 grants；新 private 使用绑定逻辑身份的 v2
  partition，并加入逻辑身份过滤、设备来源和独立幂等键。旧 private/null-agent
  不被新 grant 自动公开。`policy_admin.py` 提供 validate/revoke/rotate，私有文件、
  原子替换、fsync、管理锁以及拒绝覆盖 Token 输出。
- `owner_lifecycle.py` 接纳不可变事件 ID、可信 grant 和显式隐私规则，先记录 durable
  plan，再复用 Runtime 提取、去重、操作和恢复。`owner_llm.py` 为非 Hermes 提供
  明确配置的 OpenAI-compatible 模型入口；没有默认访问外部模型。
- Pi adapter 只使用官方 hooks 和当前获授权会话中自己的 outbox entries；不爬取
  历史聊天。事件绑定 Owner/凭据摘要，换凭据不会把旧事件发往新空间；旧绑定事件
  不阻塞当前绑定的发送。召回通过 ephemeral context 注入，不进入历史 transcript。
- Native RPC 保留 Owner 的 host profile context，防止 host-provided embedder
  在 RPC 线程误用默认/外来 profile 的 secret scope。
- Native private 召回缓存绑定 Runtime mutation revision，并在交付前重新授权；远程
  更新、删除和撤权后不交付旧缓存。已接受事件即使在生成 operation 前提取失败，也会
  保持 Owner 恢复门禁，避免绕过失败事件继续写入。
- 最终隔离复查新增两个先失败的回归：legacy private 精确哈希误去重、语义更新误提升。
  lifecycle 的哈希/语义去重均强制 protocol/space/visibility 过滤，private 再加 logical
  agent；写入前复核目标归属，拒绝给旧未标记记录加共享权限。普通旧 Runtime 路径兼容。

部署、安全和回滚的完整说明见 [remote-owner.md](../shared-memory/remote-owner.md)。
三个后端配置位于 `docs/examples/universal-owner/`；MCP 仓库提供 Codex、DSH、
WorkBuddy/generic stdio 的 remote-preview 配置及 Pi 的扩展说明。

## 4. 宿主能力矩阵

| 宿主 | 接入 | 自动记忆证据 | 实际等级 |
|---|---|---|---|
| Hermes | 单 Native Provider，可托管 Owner；远程设备可选择显式 MCP fallback | 实际 MemoryManager callback + Native Runtime；宿主 LLM facade 为确定性 fixture | 真实宿主 runtime 已测；UI/真实主模型未测 |
| Pi 1.3.0 | 官方 extension；HTTPS MCP/Owner event bridge | 实际新 session、context/turn hooks；完整 Hermes 交接进程测试 | 真实宿主 runtime 已测；UI/真实生成模型未测 |
| Codex | 本机 shared-mcp / 远程 remote-mcp stdio | 未增加官方 lifecycle adapter | explicit MCP / 配置示例 |
| DSH | 既有官方 MCP client + remote bridge 配置 | 未增加 lifecycle adapter | explicit MCP；既有发行版集成测试保留 |
| WorkBuddy | 既有 stdio connector + remote bridge 配置 | 未增加 lifecycle adapter | explicit MCP / 配置示例 |
| OpenCode | 保留既有显式 MCP 路径 | 没有把旧版文档 Hook 假设成 v2 Hook | explicit MCP；自动模式未支持 |

Pi 1.1.0 曾解析到部分 1.2.0 子包并缺失导出，实际 Host 测试失败。已更新到
官方最新 Pi 1.3.0 的一致版本族并锁定依赖，随后真实 session 测试通过。

## 5. 本地测试证据

| 验证 | 结果 |
|---|---|
| Runtime 开发前基线，Python 3.14 + 实际 Hermes checkout | 603 PASS / 7 SKIPPED |
| Runtime 完整回归（SDK 1.30.0） | 最终 646 PASS / 8 SKIPPED / 1 已知 warning，包含 legacy 隔离新增 2 项 |
| SDK 2.3.0 standalone，Python 3.11 | 最终 151 PASS / 0 SKIPPED，包含 legacy 隔离新增 2 项 |
| Native/共享召回最后增量回归 | 35 PASS；含慢 Embedding、用户切换、远端删除/撤权失效、Native 存储失败门禁 |
| Hermes → Pi 独立进程 HTTPS 自动交接 | Python 3.14/SDK1 和 Python 3.11/SDK1、SDK2 均 PASS；最终隔离修复后 SDK2 再跑 1 PASS |
| Pi hook 合同 + 实际 Pi session | 6 PASS；含离线重试、绑定隔离、完整 provenance、private capture/project recall 独立凭据 |
| Qdrant 1.15.5 Server / client 1.15.1 TLS/JWT 隔离 | 1 PASS，真实拒绝未授权集合写入 |
| 实际 Qdrant Server 新设备 private/CAS/receipt 重启 | 1 PASS；仅使用自建临时容器和合成 collection |
| 两网络 namespace 的 HTTPS MCP | SDK1/SDK2 实跑；独立认证、CRUD、CAS、回执、TLS trust、Origin、重启 |
| MCP 完整 Python 回归 | 144 PASS / 1 SKIPPED（默认未指定 unreleased Owner Root；专门宿主 lane 已单独实跑 PASS） |
| Skill 结构/发布/CI 合同子集 | 26 PASS / 23 SKIPPED；缺少对应已安装 Runtime 的 gated 合同未冒充执行 |
| Ruff | 修改后的 Runtime 全库 check / format PASS |

Runtime 通用扫描的八个跳过项为：三个未指定 CPU 模型缓存的真实模型用例、一个
未提供独立 TLS/JWT deployment 的用例、两个未指定 server URL 的用例、一个
secret-gated Cloud 用例、一个未指定 disposable Server 的新设备身份用例。TLS/JWT 和实际 Server 已通过独立 disposable gate；
不能把这些独立结果说成通用扫描里跳过的测试已执行。另有一个旧测试使用 Embedded
模拟 server/cloud alias 时的 payload-index warning，它不是 Cloud 验证。

典型复现命令（环境路径按本机部署替换）：

```sh
PYTHONPATH=/path/to/hermes-agent .venv/bin/python -m pytest -q
.scratch/sdk2/bin/python -m pytest tests/standalone -q
.venv/bin/python scripts/test_remote_mcp_docker.py 'mcp>=1.30,<2'
.venv/bin/python scripts/test_remote_mcp_docker.py 'mcp>=2,<3'
QDRANT_DEVICE_FIXTURE_URL=http://127.0.0.1:PORT python -m pytest tests/security/test_device_server.py -q
QDRANT_OWNER_RUNTIME_ROOT=/path/to/runtime PYTHONPATH=/path/to/hermes-agent python -m pytest tests/test_native_pi_handoff.py -q
node --test tests/test_pi_owner_extension.mjs tests/test_pi_owner_host.mjs
```

## 6. CI 和辅助审查

已执行并成功的 [Runtime Shared destination security run](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39288190054)
对应 `2fdbbea`，检查了具体 steps，而不只看 workflow success：SDK1、SDK2 的真实
双网络 TLS MCP step、artifact upload，以及受保护 Owner/Qdrant TLS/JWT step 均成功。
后续补强的双设备网络场景、实际 Server 新身份 lane 和最后提交应以新 run 为准。

后续 [7fe3eb1 安全矩阵](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39292762051)
也已 PASS，逐 job 确认了实际 Qdrant Server 设备身份、protected TLS/JWT，以及
SDK1/SDK2 两网络 HTTPS MCP 的执行。该提交 packaging 和 optional embedding 工作流
成功；最新 CI 仍运行，standalone/platform 排队。最后两个安全修复不能借用早期绿灯。

[Runtime CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39288189665)、
[standalone matrix](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39288189663)、
[platform matrix](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39288189695)、
[MCP cross-harness](https://github.com/cnkang/qdrant-memory-mcp/actions/runs/39288658598)
和 [Skill validation](https://github.com/cnkang/skills/actions/runs/39288657195)
在记录时部分仍排队/运行，不宣称整条最新矩阵 PASS。新增工作流没有读取 Cloud Secret；
Cloud 继续使用既有受保护 credentialed workflow。

最后功能快照 `903a539` 的 [安全矩阵](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39294615806)
及 [CI](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39294615899)
仍排队/运行；MCP `eecaf67` 的 [cross-harness](https://github.com/cnkang/qdrant-memory-mcp/actions/runs/39294699603)
同样排队。MCP 该提交将源码验收固定到完整 Runtime SHA `89a3744fb716ac90edeb3dfdb01f59e5efcbf280`，
包含最终隔离和恢复门禁，不让 CI 意外检出正式 0.4.0 造成假通过。

使用了 [CodeRabbit code-review skill](https://www.coderabbit.ai/)
对应插件辅助审查。第一轮 1 个 major：共享 I/O 阻塞 private；改为有界等待、单在途
请求、丢弃晚到结果。第二轮 Runtime 2 个 major：未认证消耗全局额度、共享授权失效
连带阻断 private；均先复现失败测试再修复。MCP 2 个 major：旧绑定 outbox 阻塞新
事件、Host checkout 使用短 SHA；前者补回归并修复，后者在报告到达前已改成完整 SHA。
没有把人工发现归因于 CodeRabbit，也没有盲执行评审建议中的命令。
最后一轮 Runtime 另报 1 个 major：提取失败、尚无 operation 时未阻断 Native Owner。
已用失败回归复现并修复（`fc7423a`），并在 SDK1 全量 644 项及 SDK2 149 项中通过。
最终 legacy 隔离问题为人工复查发现，不计入 CodeRabbit 的 6 项有效建议。

## 7. 真实模型与检索质量

真实本机 `qwen3:8b`，digest `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`，
10 条固定匿名提取诊断：precision 0.8571，正例 case recall 1.0，case success 0.8，
1 次结构化提取失败，p50 8279 ms / p95 20350 ms。**FAIL**；未声称满足生产自动提取
质量。初次输出未记录失败细分类，不能据此断言是网络、模型还是输出长度问题。
诊断脚本随后补充了 dataset/prompt/schema fingerprint、错误类型和非零失败退出码，
为下一轮固定基线提供可追踪证据，不能把后补字段冒充首轮已测数据。

结束前使用补强后的脚本完整复跑：仍 FAIL，precision 0.8571、positive case recall 1.0、
case success 0.8、1 次 `JSONDecodeError`，退出码 1；p50 9101 ms / p95 21968 ms。
这是结构化输出解析失败的证据，尚不能区分模型截断、格式不遵从等更细根因；
没有用放宽 JSON/schema 校验或吞掉提取失败来获得绿灯。

本轮可复核指纹：

- dataset `2e75f289a9fb44822db92a079a47afc4268bc3835298b1f59644b67498f8c74a`
- prompt code `4e25216211aa264380dffeb461c99351a38c18de185a7efa53036874edaa7fc5`
- schema `fefe6d2d6a321b8ff716c32df07e4527f8779ffdfe52065bfb25e627dbbedd3c`

真实本机 `qwen3-embedding:4b`，2560 维，fingerprint
`8b5dffffa9f5477f8d0727a7b6b50e4b755e665a3ccb6ef0b500af46614a6b91`。
复用 `evaluate_shared.py` 的 21 条写入、24 个 query 匿名 pilot corpus：

| 指标 | private | shared |
|---|---:|---:|
| Recall@5 | 0.9861 | 0.9861 |
| MRR | 0.9306 | 0.9306 |
| 写入 p50 / p95 ms | 56.10 / 96.25 | 72.84 / 103.92 |
| 搜索 p50 / p95 ms | 55.70 / 69.35 | 65.53 / 93.98 |

shared scope leakage = 0，空 backlog 重启 326 ms，一个 pending request 重启约 391 ms。
仅 2 次重复 reader 采样，不作为并发 p95 生产承诺。执行时还复现并修复了旧评测脚本
不满足 protected CLI cwd/canonical-path 要求的问题；没有取消任何安全校验。

## 8. 安全、兼容性和剩余事项

默认仍 private/local，所有新共享、Native Owner、公网代理和自动 capture 都显式
opt-in。单 Owner、CAS、Ledger、durable plans/receipts、恢复门禁、destination claims、
embedding/schema fingerprint 没有被绕过。服务凭据按设备独立；身份/权限来自服务端。
临时测试容器、证书、合成 collection 已按各自 fixture 清理；没有移除真实 lock/claim/WAL。

仍未完成或未验证的 DoD：

1. OAuth/OIDC、PKCE、Protected Resource Metadata、浏览器 MCP 模式：未实现，不能
   对外宣称完整 MCP OAuth 互操作。当前只支持受控预配置服务凭据。
2. 实际公网部署、过期证书等全部负向组合、所有 Host UI、完整混合 SDK 客户端矩阵：
   UNVERIFIED。配置示例不是这些验证的替代品。
3. Cloud 新 grant/lifecycle/HTTPS 组合：SKIPPED，没有使用生产/收费资源凭据。
   Cloud 数据库存储可达不证明 HTTPS MCP 通过，反过来也不证明 Cloud 通过。
4. Native 作为独立远程自动客户端、Codex/DSH/WorkBuddy/OpenCode 官方自动 adapter、
   更丰富的通用生命周期事件及待执行事件管理/取消 API：未实现。MCP fallback 保持 explicit。
5. 真实模型提取质量仍 FAIL；需要扩大固定匿名多轮数据，涵盖更新过期、删除撤权、
   多语言干扰及错误注入率，并固定模型、prompt、schema、embedding 和数据集版本。
6. 最新提交完整 CI 矩阵仍需完成；Windows public/protected admin 故意不支持并 fail closed。
7. 多 Owner、分布式 fencing、远程 HA、自动迁移和管理 UI 不在本次安全支持范围。

未把这些未完成项包装成“全部任务完成”。

## 9. 提交与后续 PR 顺序

已经按能力提交：`fba4d32` 只读召回、`86b15c7`/`ae5d360` 设备身份、`0864784`
durable lifecycle、`f7e9480` MCP transport、`51d6338` Native Owner、`3233ee5`
安全修复、`4ec2125` Host profile context；MCP `5c5235c` Pi/Host 验收；Skill
`e04ba35` 预览能力边界。后续增量提交以分支日志为准。

建议开 stacked Draft PR，而不是一次合并全部 Runtime：#63 Reader → Native 只读召回
（包含竞态和预算修复）→ Identity/凭据治理 → Transport + 安全回归 → Native Owner/
生命周期 → MCP Pi + CI → Skill 文档。最终安全修复必须随对应前置 PR 一起审查，不能
合并早期不含修复的中间快照。#64 作为独立依赖更新审核；#65 的上游 Action 更新已同步。

发布顺序必须 Runtime 新版本 → 单独更新 MCP 正式依赖/Managed lockfile → Skill 支持版本。
当前版本号和正式发行 pins 没有改动，未创建 tag/release/publish、未合并任何 PR。

可审阅分支：

- [Runtime](https://github.com/cnkang/hermes-plugin-qdrant-memory/tree/codex/universal-owner)
- [MCP / Pi adapter](https://github.com/cnkang/qdrant-memory-mcp/tree/codex/owner-lifecycle-adapters)
- [Skill](https://github.com/cnkang/skills/tree/codex/universal-owner-guidance/qdrant-memory)

最终本地确定性/协议测试通过不抵消上述质量 FAIL、Cloud SKIPPED 和部署 UNVERIFIED。

后续独立 PR 验收重点：先关闭真实模型结构化输出与负例误记问题，再扩充匿名多轮质量
数据集；为另一个有稳定官方 hooks 的非 Hermes 宿主补适配及真实宿主验收；补通用事件
查询/取消的授权合同；OAuth 独立设计与互操作测试；最后在授权的 disposable Cloud
和公网环境完成矩阵。所有这些均需独立证据后才能提升支持等级。
