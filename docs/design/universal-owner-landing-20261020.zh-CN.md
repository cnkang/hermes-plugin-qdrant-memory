# Universal Owner 研发、验收与合并记录
本文基于 2026-10-20 的实际代码、测试和 GitHub 状态；此前 progress 报告是历史快照。用户现已允许按依赖顺序合并合格 PR，独立依赖 PR #64 保持不动。没有发布正式版本、使用生产凭据、修改线上环境或执行数据迁移。

## 完成情况与落地状态

| 阶段 | 代码和设计成果 | 验收边界 |
|---|---|---|
| Phase 0 | 三仓库 Git/Release/未合并 PR 审计、单 Owner ADR、授权与威胁模型 | 已实际核对；本地既有 ahead 提交保留 |
| Phase 1 | 本机跨 Harness 共享召回；Hermes Native 复用原 Provider、Runtime 和生命周期 | #66 已 rebase 合并；Native 随 #69 落地 |
| Phase 2 | 原生 HTTPS + 官方 MCP Streamable HTTP；stdio bridge；安全监听、TLS、Host/Origin、限流及关闭恢复 | #68 最终 main 基线 CI 待完成；受控 provisioned-credential MVP，不声称支持 OAuth/OIDC |
| Phase 3 | 同一逻辑 Agent 双设备身份、不同 Agent 授权项目共享、设备撤销/轮换、私有隔离、CAS/幂等及恢复 | #67 已 rebase 合并 |
| Phase 4 | 实际 Hermes MemoryManager 与 Pi 1.3 生命周期 hooks、outbox、UTF-8、单飞排空和凭据快照 | MCP #32 实际 Host 与 Node 测试通过；待 Runtime 依赖落地、最终 pin 和 CI |
| Phase 5 | 三平台/SDK1/SDK2/真实 HTTP 门禁、可选 Cloud、实际本机模型、质量评分与部署文档 | #70/#71 已实现；最终 CI、合并和跨仓库 SHA 固定仍在推进 |

实际合并：#63 于 2026-10-19 22:55:07 UTC 合并，提交 `ebfceefc146c3b0f038d4f871d6ff7c380272da8`；#66 rebase 合并为 `e909eac28a64b073cbca6e4aaed04b55df1e5843`；#67 在精确 HEAD `94accba319eecc0c14b31b35d6c111af1c715385` 的 80 SUCCESS、1 SKIPPED 后 rebase 合并为 `d6da3cddd06d1c47b711ff0633ecddf4a2ae8b54`。没有管理员绕过、review dismissal 或保护规则变更。

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
| 当前 Runtime 全量，Python 3.11 / SDK2，实际 Hermes checkout | **PASS：719 passed、9 skipped**，150.70 秒；1 条 Embedded 不支持 payload index 的提示 |
| 替换逻辑、身份评分、runner、授权拒绝诊断定向测试 | **PASS：35 tests**；缺失逻辑身份的评分回归修复前明确 FAIL |
| Native Windows 修复的 POSIX 回归 | **PASS：14 tests**；Windows 仍须最终 CI |
| 实际 Hermes → verified HTTPS MCP → Pi | **PASS：1 test**；Pi Node hooks/并发/UTF-8 **PASS：11 tests** |
| #70 Host 排空后新完整模型基线 | **PASS：24 次 capture、32 条 query，重启再测全部 query**；precision/positive recall/Recall@5/MRR 均 1.0；scope leak/provider failure 为 0；重放、重启安全，重启新增模型调用为 0 |
| #70 新证据源文件校验 | **PASS：42 个源文件 SHA256 与对应 checkout 一致**；`owner-model-quality-drained-evidence.json` |
| #71 新跨逻辑 Agent 完整模型证据 | **PASS：baseline_executed=true、四项基线指标均 1.0、11 项 mutation 全部通过**；writer=pi、reader=hermes；旧事实召回 0/8、CAS 一次成功一次冲突、恢复成功率 1.0；`owner-mutation-identity-evidence.json`，43 个源码指纹全部匹配 |
| Ruff / 定向格式校验 | **PASS**；提交钩子也通过 |
| scoped Cloud | **SKIPPED**：未提供隔离测试凭据，没有借用生产 key |
| 专用真实模型 GitHub Runner | **UNVERIFIED**：已实现安全的手动 lane，未配置或运行该远端 runner |
| 公网部署、服务安装、真实用户 UI | **UNVERIFIED**：本机 CA 验证的 HTTPS 测试不冒充这些验收 |

全量测试首次未带 Hermes 源路径导致 17 个 collection errors；补全实际 Host 的 PYTHONPATH 后完整通过。环境配置错误没有计为产品测试 PASS，也没有修改代码以掩盖。

模型使用已安装的本机 `qwen3:8b` 和 `qwen3-embedding:4b`，没有下载模型或调用生产服务。固定匿名语料包含 24 轮、16 个标注事实、8 个负例和 32 个查询，另有独立 mutation corpus。证据包含语料、prompt、schema、模型和源文件指纹。最新完整运行有 29 次模型观测、64 次检索观测：模型 p50/p95 为 12.74/19.16 秒，检索为 53.70/67.57 毫秒，mutation capture p50/p95 为 17.01/38.18 秒；scope leak、provider failure、重启新增模型调用均为 0。小语料结果不构成生产准确率或 SLA 保证。

## 安全与兼容性

只有一个活跃 Memory Owner/Runtime/Ledger 负责 Qdrant 写入，保留 Embedded、Self-hosted 和 Cloud 三种后端。Agent 不持数据库写 key，不绕过 Owner；Owner/逻辑 Agent/设备/空间/可见性/权限来自管理员可信 grant。私有 v2 分区、失效/撤销、CAS、幂等回执和恢复门控保持启用。HTTPS + Streamable HTTP 不要求 SSH/VPN，也不强制 Cloud。

新 remote/lifecycle 模式为 opt-in source preview。OAuth/OIDC、browser authorization 和无真实 hooks 的 Harness 自动模式不在已验收范围。其他 Harness 可用显式 MCP/辅助接入，不能把 Skill 文本称为自动生命周期实现。原 Ledger 候选向后兼容；未改存储/embedding schema，没有自动迁移。

版本号不变：Runtime 0.4.0、MCP 源树 0.2.2、Skill 0.3.1。已核对的 latest Release 为 Runtime v0.4.0、MCP v0.2.1、Skills skills-20261019.1，源树版本不等于已发布能力。最终文档/CI 将使用可追溯的完整 immutable SHA，不能把移动分支或旧绿色 head 当作最终证据。

## 后续顺序与尚未完成项

1. #68 在 #67 已合并的 main 上重放，文件树完全一致；以 force-with-lease 精确校验旧 HEAD 后推送，新 CI 必须完成。
2. 依次处理 #69、#70、#71 的最终基线、有效 review 和 CI。旧的暂缓 #69 要求已被用户最新合并授权替代。
3. Runtime 核心依赖落地后，更新 MCP #32 的兼容 Runtime SHA，验证后合并；Skill #29 固定已验证的 Runtime/MCP SHA。区分最小兼容测试 pin 与推荐版本，避免跨仓库循环更新。
4. 为优先落地前置 PR，暂时取消了后置 #69–#71 的旧/未定基线 CI；取消不是 PASS，最终必须重新运行。#64 未修改、未合并，main CI 未取消。
5. 外部环境才能完成的 Cloud、专用 runner、公网/用户 UI 保持明确 SKIPPED/UNVERIFIED。OAuth/OIDC 若开展，作为独立后续 PR，不能用当前受控 MVP 的测试替代。

证据入口：[Runtime #68](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/68/checks)、[#69](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/69/checks)、[#70](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/70/checks)、[#71](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/71/checks)、[MCP #32](https://github.com/cnkang/qdrant-memory-mcp/pull/32/checks)、[Skill #29](https://github.com/cnkang/skills/pull/29/checks)。这些链接显示最新状态，不替代上面精确提交与实际结果的区分。
