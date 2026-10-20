# Universal Owner 研发、验收与合并记录
本文基于 2026-10-20 的实际代码、测试和 GitHub 状态；此前 progress 报告是历史快照。用户已允许按依赖顺序合并合格 PR。独立依赖 #64 实际为 CLOSED，其替代 #72 已由维护者合并为 `51200b79bdd7791c95bbf2eeb20c74c19b1a3fb5`；没有改写两者历史。没有发布正式版本、使用生产凭据、修改线上环境或执行数据迁移。

## 完成情况与落地状态

| 阶段 | 代码和设计成果 | 验收边界 |
|---|---|---|
| Phase 0 | 三仓库 Git/Release/未合并 PR 审计、单 Owner ADR、授权与威胁模型 | 已实际核对；本地既有 ahead 提交保留 |
| Phase 1 | 本机跨 Harness 共享召回；Hermes Native 复用原 Provider、Runtime 和生命周期 | #66 已 rebase 合并；Native 随 #69 落地 |
| Phase 2 | 原生 HTTPS + 官方 MCP Streamable HTTP；stdio bridge；安全监听、TLS、Host/Origin、限流及关闭恢复 | #68 最终 main 基线 86 SUCCESS、1 SKIPPED 后已 rebase 合并；受控 provisioned-credential MVP，不声称支持 OAuth/OIDC |
| Phase 3 | 同一逻辑 Agent 双设备身份、不同 Agent 授权项目共享、设备撤销/轮换、私有隔离、CAS/幂等及恢复 | #67 已 rebase 合并 |
| Phase 4 | 实际 Hermes MemoryManager 与 Pi 1.3 生命周期 hooks、outbox、UTF-8、单飞排空和凭据快照 | MCP #32 实际 Host 与 Node 测试通过；待 Runtime 依赖落地、最终 pin 和 CI |
| Phase 5 | 三平台/SDK1/SDK2/真实 HTTP 门禁、可选 Cloud、实际本机模型、质量评分与部署文档 | #70/#71 已实现；最终 CI、合并和跨仓库 SHA 固定仍在推进 |

实际合并：#63 于 2026-10-19 22:55:07 UTC 合并，提交 `ebfceefc146c3b0f038d4f871d6ff7c380272da8`；#66 rebase 合并为 `e909eac28a64b073cbca6e4aaed04b55df1e5843`；#67 在精确 HEAD `94accba319eecc0c14b31b35d6c111af1c715385` 的 80 SUCCESS、1 SKIPPED 后 rebase 合并为 `d6da3cddd06d1c47b711ff0633ecddf4a2ae8b54`。没有管理员绕过、review dismissal 或保护规则变更。

#68 在精确 HEAD `36f268a0394afb722e905c992c69c47ea5be0aad` 的 86 SUCCESS、1 Sourcery SKIPPED 且无未解决 review 线程后，于 2026-10-20 06:14:50 UTC rebase 合并为 `aeeb09da21f30d5b2f8306553763b15241fc4b0c`。#69 随后由维护者更新到 #72 的 main，远端为 `7eaf12464a8e3c378859f988dd10f5ce6f5047d5`，保留其取消任务时跳过 gate 的修复。本地追加 Native 未准入事件显式离线处置提交 `f94c166`，完整回归 702 PASS、9 SKIPPED。它仍待依赖整合、最终 CI 和有效复审。MCP #32 的 Runtime pin 目前仍为先前候选 `38fddaf3e1d36b6f5453a9378bf8e64efc154c45`，必须在合并前更新，不能视为最终 pin。

质量补充已实际验证：新增 Pi 真正模型输入的观测，单独计算已标注事实的错误记忆注入率，不用检索 Recall 或 extraction precision 代替。该质量分支快照全量 720 PASS、9 SKIPPED；36 项定向测试在 Python 3.11 和 3.14 均 PASS。最终 `owner-model-context-reviewed-evidence.json` 为 PASS，12 次实际模型输入、38 条注入记忆，错误及越权注入均为 0；四项基线指标均 1.0、11 项 mutation 全部通过。43 个源码 SHA-256 已再次逐项核对匹配。此证据对应记录的质量分支源码，后续整合新的 Runtime 改动后需要重新绑定源码和验收。

依赖补充 #74：#72 的 Hub 2 / Pillow 13 与官方 FastEmbed 0.9.0 声明不兼容；降级 Pillow 12.3.0 虽能安装却触发 Snyk 16 个问题，已撤销该方案。按用户最新指示，候选 `fa9f3833cf7a2dc96db47cbba1f089db95499448` 使用源 wheel 固定 SHA-256、仅修改分发元数据的 `0.9.0+qm.pillow13` 临时构建，固定 Pillow 13.0.0、Hub 1.33.0。最终候选三平台 GitHub CPU 均 70 契约 + 3 真实模型 PASS，Snyk 核心 20 / CPU 38 依赖均报告 0 问题；安全阈值、哈希及 pip check 均保留。英文注释说明上游 fastembed#830 发布后的移除条件。CodeRabbit 两项有效反馈均修复，最后两文件修正复查 0 issues；其余 CI 和合并仍在收尾。

## 本轮发现与修复

1. 核实维护者修复：Gateway 子进程测试固定实际 checkout、PYTHONPATH 和读取线程收尾；Native 工具授权不再回退到未授权 scope。进一步依据 CodeRabbit 发现 Native 自动事件仍有 scope 回退，先用 turn/builtin/checkpoint 三项失败测试复现，再统一拒绝。
2. MCP #32 四项反馈全部有效并修复：stdout UTF-8 流解码与增量字节上限；prompt 不等待网络、生命周期单飞排空；WorkBuddy 安全变量引用；服务启动使用独立 deadline。另固定异步凭据快照及 session generation，避免旧 outbox/recall 越身份交付。
3. Windows CI 发现受保护模式先拒绝不支持的 POSIX 权限验证，端口测试却期望更晚的 ValueError。只修正拥有该用例的 #69 测试预期，并断言未创建向量目录；没有跳过测试或削弱安全校验。
4. 实际模型 FAIL 暴露明确替换事实仍被 ADD 的问题。新增内部 required `replacement_intent`；仅明确替换时审查同 scope 的最多五个候选，仍须关系判定 SUPERSEDES 才更新。原始用户陈述作为不可信证据参与判断，命名主体及事实限定词必须保留。普通候选维持原阈值与策略，历史 Ledger 缺少标记时默认 false。
5. 实际 Host 异步交付有验收竞态：现在先等待公开的 `MemoryManager.flush_pending()`，再等待 Provider idle；不提高网络或模型超时。重启比较保留所有持久字段，只排除计算产生的 score。
6. 复核发现 mutation 评测将同一逻辑 Agent `pi` 的两个凭据误标为跨 Agent。已撤回旧 PASS 的该项结论，保留原始 JSON 供追溯。现在由独立授权的 `hermes` 读取 `pi` 的项目事实，评分器同时要求已存事实身份与实际 reader grant 身份非空且不同；缺失或同身份必须 FAIL。新的完整评测已 PASS，43 个源文件指纹匹配。
7. CodeRabbit 的诊断自相似度、初始失败证据说明、按 ID 核对旧候选相似度均已修正。其“首项也应低于阈值”的建议不适用于该 fixture：首项故意是另一高分候选；保留该情形，以覆盖低分旧候选位于后续结果的真实路径。

## 实际验证

| 检查 | 结果 |
|---|---|
| 质量分支观测器快照全量，Python 3.11 / SDK2，实际 Hermes checkout | **PASS：720 passed、9 skipped**，157.26 秒；1 条 Embedded 不支持 payload index 的提示 |
| 替换逻辑、身份评分、runner、授权拒绝诊断定向测试 | **PASS：35 tests**；缺失逻辑身份的评分回归修复前明确 FAIL |
| Native Windows 修复的 POSIX 回归 | **PASS：14 tests**；Windows 仍须最终 CI |
| 实际 Hermes → verified HTTPS MCP → Pi | **PASS：1 test**；Pi Node hooks/并发/UTF-8 **PASS：11 tests** |
| #70 Host 排空后新完整模型基线 | **PASS：24 次 capture、32 条 query，重启再测全部 query**；precision/positive recall/Recall@5/MRR 均 1.0；scope leak/provider failure 为 0；重放、重启安全，重启新增模型调用为 0 |
| #70 新证据源文件校验 | **PASS：42 个源文件 SHA256 与对应 checkout 一致**；`owner-model-quality-drained-evidence.json` |
| #71 新跨逻辑 Agent 完整模型证据 | **PASS：baseline_executed=true、四项基线指标均 1.0、11 项 mutation 全部通过**；writer=pi、reader=hermes；旧事实召回 0/8、CAS 一次成功一次冲突、恢复成功率 1.0；`owner-mutation-identity-evidence.json`，43 个源码指纹全部匹配 |
| #71 实际模型上下文最终观测 | **PASS：12 次输入、38 条记忆、0 错误、0 越权注入**，错误注入率 0；基线与 mutation 同时运行并 PASS；`owner-model-context-reviewed-evidence.json` 的 43 个源码指纹全部匹配 |
| Ruff / 定向格式校验 | **PASS**；提交钩子也通过 |
| scoped Cloud | **SKIPPED**：未提供隔离测试凭据，没有借用生产 key |
| 一次性真实 Qdrant Server | **PASS：3 tests**，REST/gRPC、索引、scope、变更、迁移、容器重启、双设备私有共享/CAS/回执恢复；容器清理成功 |
| 一次性 TLS/JWT 数据库账号隔离 | **PASS：1 test**，Owner 允许写，缺失/只读/无关集合凭据被拒绝；使用现有 CI 脚本，容器及短期凭据已清理 |
| 专用真实模型 GitHub Runner | **UNVERIFIED**：已实现安全的手动 lane，未配置或运行该远端 runner |
| 公网部署、服务安装、真实用户 UI | **UNVERIFIED**：本机 CA 验证的 HTTPS 测试不冒充这些验收 |

全量测试首次未带 Hermes 源路径导致 17 个 collection errors；补全实际 Host 的 PYTHONPATH 后完整通过。环境配置错误没有计为产品测试 PASS，也没有修改代码以掩盖。

再次全量复核为 **719 PASS、9 SKIPPED，140.65 秒**。9 个跳过项具体为：3 个需要已准备 CPU 模型缓存，3 个需要实际 Qdrant Server，2 个需要 scoped Cloud，1 个需要独立数据库账号隔离部署。随后使用 CI 同一固定镜像 `qdrant/qdrant@sha256:0fb8897412abc81d1c0430a899b9a81eb8328aa634e7242d1bc804c1fe8fe863`，已单独补跑 Server 的 3 项和 TLS/JWT 隔离的 1 项且全部 PASS；不把全量测试的原始 skipped 数字改写为通过。

本机补跑先暴露两种测试准备问题：遗漏 Hermes 源路径会使两个契约用例无法导入 `utils`；Docker Desktop 的随机发布端口在 restart 后会改变，实测 `50560→50563`，导致 REST 继续访问旧地址失败。固定空闲的 loopback REST/gRPC 端口并补全 Host 路径后，原有断言和超时不变，3 项全部通过。只创建和清理本次一次性容器，没有操作既有容器。

保留的警告及边界：本地 qdrant-client 1.19.1 与固定 Server 1.15.5 超出 SDK 推荐的小版本差；重启准备期间还会出现暂时无法读取 server version 的提示。没有关闭兼容性校验或过滤警告。上述实际 REST/gRPC 和隔离 API 路径已经 PASS，但不能外推为所有新 SDK API 均兼容旧 Server。#64/#72 的状态按真实远端记录，不据此消除版本差警告。Embedded payload index 提示则由刻意覆盖 server/cloud alias 的内存后端测试产生，不是本机后端实际创建了有效索引。

质量评估使用已安装的本机 `qwen3:8b` 和 `qwen3-embedding:4b`，没有为该评估下载模型或调用生产服务。固定匿名语料包含 24 轮、16 个标注事实、8 个负例和 32 个查询，另有独立 mutation corpus。证据包含语料、prompt、schema、模型和源文件指纹。最终上下文观测运行有 29 次模型、64 次检索观测：模型 p50/p95 为 12.70/17.62 秒，检索为 56.36/70.47 毫秒，mutation capture p50/p95 为 17.21/35.86 秒；scope leak、provider failure、重启新增模型调用均为 0。小语料结果不构成生产准确率或 SLA 保证。另行验证 Pillow 兼容性的 CPU 模型准备使用独立临时缓存；本地触及既有下载时限，最终由 GitHub 三平台真实模型 CI 验收通过。

## 安全与兼容性

只有一个活跃 Memory Owner/Runtime/Ledger 负责 Qdrant 写入，保留 Embedded、Self-hosted 和 Cloud 三种后端。Agent 不持数据库写 key，不绕过 Owner；Owner/逻辑 Agent/设备/空间/可见性/权限来自管理员可信 grant。私有 v2 分区、失效/撤销、CAS、幂等回执和恢复门控保持启用。HTTPS + Streamable HTTP 不要求 SSH/VPN，也不强制 Cloud。

新 remote/lifecycle 模式为 opt-in source preview。OAuth/OIDC、browser authorization 和无真实 hooks 的 Harness 自动模式不在已验收范围。其他 Harness 可用显式 MCP/辅助接入，不能把 Skill 文本称为自动生命周期实现。原 Ledger 候选向后兼容；未改存储/embedding schema，没有自动迁移。

版本号不变：Runtime 0.4.0、MCP 源树 0.2.2、Skill 0.3.1。已核对的 latest Release 为 Runtime v0.4.0、MCP v0.2.1、Skills skills-20261019.1，源树版本不等于已发布能力。最终文档/CI 将使用可追溯的完整 immutable SHA，不能把移动分支或旧绿色 head 当作最终证据。

## 后续顺序与尚未完成项

1. #68 的新基线检查和 rebase 合并已完成；后续分支同样保留备份、比对文件树，并以 force-with-lease 校验远端旧 HEAD。
2. 依次处理 #69、#70、#71 的最终基线、有效 review 和 CI。旧的暂缓 #69 要求已被用户最新合并授权替代。
3. Runtime 核心依赖落地后，更新 MCP #32 的兼容 Runtime SHA，验证后合并；Skill #29 固定已验证的 Runtime/MCP SHA。区分最小兼容测试 pin 与推荐版本，避免跨仓库循环更新。
4. 为优先落地前置 PR，曾取消后置 #69–#71 的旧/未定基线 CI；取消不是 PASS，最终必须重新运行。没有修改 #64 或取消 main CI；维护者实际合并的是其替代 #72。
5. 外部环境才能完成的 Cloud、专用 runner、公网/用户 UI 保持明确 SKIPPED/UNVERIFIED。OAuth/OIDC 若开展，作为独立后续 PR，不能用当前受控 MVP 的测试替代。

证据入口：[Runtime #68](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/68/checks)、[#69](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/69/checks)、[#70](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/70/checks)、[#71](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/71/checks)、[MCP #32](https://github.com/cnkang/qdrant-memory-mcp/pull/32/checks)、[Skill #29](https://github.com/cnkang/skills/pull/29/checks)。这些链接显示最新状态，不替代上面精确提交与实际结果的区分。
