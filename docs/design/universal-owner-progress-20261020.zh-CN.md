# Universal Owner 研发与验收进展

日期：2026-10-20，Asia/Singapore。本文记录本轮代码与远端核验事实；历史绿色 CI 不代替新提交检查。

## 本轮结果

- 此快照记录对应源码与测试结果，不代表最新提交状态。
- Runtime [#66](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/66) 已通过正常 **rebase merge** 合入 main；合并提交 `e909eac28a64b073cbca6e4aaed04b55df1e5843`，时间 2026-10-20 04:19:33 UTC。合并前精确 head `5bb615a1fa84928480988ccc8fbc2252e83df053` 有 80 项成功检查、1 项跳过，唯一 CodeRabbit 有效反馈已修复并关闭线程。
- GitHub 对 #66 仍显示来自旧 head 的 CHANGES_REQUESTED；没有把它描述成新批准，也没有 dismiss review。仓库保护要求 `Required security scan gate`，没有额外批准票要求。使用 `--rebase --match-head-commit` 正常合并，未使用管理员绕过、修改保护或降低门禁。
- Runtime [#69](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/69) 在此快照中尚未合并。CodeRabbit 反馈发现的另一条 Native 生命周期授权回退路径已修复。
- MCP [#32](https://github.com/cnkang/qdrant-memory-mcp/pull/32) 四项反馈全部确认有效、修复、推送并关闭线程。修复提交 `8b206568210219674e8ea45ce606c8c3572afa57` 的 66 项远端检查成功；后续仅更新 Runtime 测试源固定版本，其新 CI 单独等待。
- 新建 Phase 5 [#70](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/70)，包含真实模型多轮自动记忆质量门禁；最终本地模型验收已 PASS，保持 Draft 等待新 head CI。

## 分阶段状态

| 正式阶段 | 已实现和验证 | 当前未完成项 |
|---|---|---|
| Phase 0：审计与架构 | 三仓库、未合并 PR、Release/版本边界及单 Owner ADR 已审查；本轮重新核验真实 Git/PR 状态 | 最终批次检查继续以精确 head 为准，不能用历史报告认定全部验收完成 |
| Phase 1：本机共享、Hermes Native | #66 共享召回已合并；Native Owner、自动事件、恢复和上下文隔离已实现 | Native 所在 #69 尚未合并，须等待最新 CI 与复审 |
| Phase 2：Remote MCP | 原生 HTTPS + 官方 Streamable HTTP；Host/Origin、令牌、并发、时限、TLS、转发头与关闭恢复测试 | #68 纳入 main 后的新 head CI；公网生产部署没有执行；OAuth/OIDC/browser 模式仍不支持 |
| Phase 3：跨设备身份 | Owner/逻辑 Agent/设备/空间授权、撤销/轮换、CAS、幂等回执、恢复及私有分区 | #67 新 base/main 检查待完成后按顺序合并 |
| Phase 4：通用生命周期 | 实际 Pi SDK 自动 hooks、持久 outbox、私有写/项目读分离、Hermes→HTTPS→Pi 实测 | #32 新 pin CI 和 Runtime 依赖；Codex/DSH/WorkBuddy 等缺少实际 hooks 的客户端仍为手动 MCP 配置，不宣称自动生命周期 |
| Phase 5：CI、质量与文档 | 真实 HTTP/跨网络与三平台矩阵、可选 Cloud lane、固定语料、真实提取/embedding、多轮与重启质量门禁 | 新 head 的远端 CI；真实模型专用 Runner 尚未配置/执行；无 scoped Cloud 凭据，Cloud 验收未执行 |

## 本轮代码变更

### 维护者后续调整的复核

- #68 的 Gateway 关闭测试使用绝对 fixture 路径、显式 checkout/PYTHONPATH，读取完整子进程诊断并在关闭管道前 join 读取线程。解决实际 Hermes 测试 cwd 与 Python `tests` 包遮蔽问题，而非放宽断言。原 head `26d37bac729b46c6c591c977be70fee02ce93feb` 的 86 项远端检查成功。
- #69 的 Native 工具调用明确拒绝非 Owner scope，不再进入本地 dispatch。准入测试提供有效 grant 和必要 destination，并用错误消息验证确实到达目标安全门控；PR #63 文档改为已合并的历史评估。相关组合本地测试 34 PASS。
- 合并 #66 后，只把 main 集成进后续栈，没有重写维护者历史或 force push。#67 base 已为 main；后续 #68→#69→#70 依赖栈保持明确。

### MCP #32

- stdout 在监听数据前启用 UTF-8 流解码，跨 chunk 中文不会损坏；增量字节计数落实 1 MiB 限制。
- prompt 阶段触发重试而不等待网络；单个 in-flight drain 和补排请求避免并发 hooks 重复发送/确认。session_start、turn_end、session_shutdown 仍等待排空。
- 异步操作固定凭据快照并复核 session generation：凭据切换不会把旧 outbox 内容交给新身份，也不会注入旧 read grant 的 recall。
- WorkBuddy 使用文档规定的 `${QDRANT_MEMORY_REMOTE_TOKEN}`；通用客户端须使用各自支持的 secret reference，或在安全继承变量时省略 env。依据：[官方 connector 文档](https://open.workbuddy.cn/en/docs/connector)。
- Gateway startup 使用独立 10 秒 deadline；CI 显式加入 UTF-8 回归测试。
- 提交：`8b20656`；源测试 pin 更新提交：`be6e653`，固定 Runtime `d13b72c1bef47bcc6b261333f38b8916bafaed1e`。

### Native 授权补充修复

CodeRabbit 重审 #69 提出 1 major issue。原 `NativeOwner.events()` 对外部 scope 返回原事件，随后 `_enqueue_event` 会绕过 Owner grant 进入 Runtime。工具与召回已拒绝该身份，自动写入却未遵守同一授权边界。

已先复现 turn、builtin memory、mixed checkpoint 三项失败，再在入口抛出 PermissionError，复用现有 Provider 的 `native_owner_unadmitted` 持久失败语义。没有另建 Runtime、直接写 Qdrant 或修改授权校验。原 Owner 写入仍成功，拒绝事件无新增记忆，跨身份事件也不能经 operator retry 被重新归属。修复提交 `d13b72c`；相关 36 PASS。

### 真实模型质量门禁

- 固定匿名语料：24 turns、16 gold facts、8 negative turns、32 retrieval queries；覆盖 Hermes 私有、Pi 私有和明确授权项目空间，使用多 session 的实际 Host hooks。
- 现有 OwnerLLM 和 HTTPEmbeddingProvider 使用已安装本机 `qwen3:8b`、`qwen3-embedding:4b`；不下载模型、不调用生产/付费凭据。Pi assistant 为固定 ack，Owner 提取与 embedding 为真实推理；不把固定 ack 宣称为完整模型 assistant 行为验证。
- 门禁：precision≥0.95、positive-case recall≥0.90、Recall@5≥0.90、MRR@5≥0.80，scope leaks/provider failures 为零，事件 replay 与重启安全，缺失证据为 FAIL。
- 初始真实运行确实记录 FAIL：原始搜索结果直接比较包含动态 score。诊断重跑确认仅 score 不同；改为核对所有返回 ID/持久化字段，重启后再执行全部 32 条查询并重新评分。没有忽略版本、正文、身份或 provenance。
- CodeRabbit 对新门禁提出 5 项反馈，均有效并已修复：重启前后模型调用计数、按 ID 对齐诊断、保留原始异常、模型探测失败也输出脱敏 FAIL，以及区分记录器错误与 Provider 错误。记录器错误不会改变被测模型返回，但会阻止门禁 PASS。
- 两个关键问题先写失败测试，修复后评分器/脚本共 18 PASS；CodeRabbit 对修复 diff 重审 **0 issues**。不把这次 diff 复审称为整个栈独立安全审计。
- 手动 CI 只面向专用、可丢弃 Linux 测试 Runner，没有 PR/fork trigger，不使用生产凭据；尚未执行的远端真实模型 lane 标为 UNVERIFIED。

## 验证证据

| 检查 | 结果与边界 |
|---|---|
| 维护者 Native/Gateway/recall 修复组合 | PASS，34 tests |
| 新 Native turn/builtin/checkpoint 授权回归组合 | PASS，36 tests；修复前 3 FAIL |
| 完整 Runtime + 评分门禁基线 | PASS，710 tests；9 SKIPPED；1 条 Embedded payload index 无效提示。此全量结果不冒充后来仅修改验收脚本的重新全量运行 |
| 历史脚本/评分回归（当时版本） | PASS，Python 3.11 和 3.14 各 18 tests，评分模块 coverage 90%；两个新增问题修复前 2 FAIL。SDK2 测试环境未安装 pytest-cov，因此覆盖率使用既有 Python 3.14 环境测量 |
| Pi Node 实际 Host/并发/UTF-8 | PASS，11 tests |
| 最新 Native 代码 Hermes→verified HTTPS→Pi | PASS，1 test，0 skip |
| Ruff / 格式 / actionlint / diff whitespace | PASS；自定义测试 Runner label 在 actionlint 配置中明确声明，没有忽略检查 |
| Skill 校验 | PASS，immutable Runtime/MCP preview pin 已同步，提交 `f066ed0` |
| 真实模型诊断运行 | PASS；重启前后四项质量指标均 1.0，scope leaks/provider failures 为 0，24 次模型调用；64 条检索观测；仅动态 score 不同 |
| 历史脚本复审后的模型运行 | PASS：四项质量指标均 1.0，scope leaks/provider failures/recording errors 为 0，replay/restart 安全，重启新增模型调用 0；24 次真实模型推理、64 条检索观测，源码 hash 对应该次运行的文件 |
| scoped Qdrant Cloud | SKIPPED：没有提供独立测试 scope/collection 凭据；没有使用生产 key 代替 |
| 真实模型 GitHub Runner、公网 TLS/服务安装、用户 UI | UNVERIFIED，不把本机 loopback TLS 或配置示例当作这些环境的验收 |

远端历史绿色证据：[#66 checks](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/66/checks)、[#68 checks](https://github.com/cnkang/hermes-plugin-qdrant-memory/pull/68/checks)、[MCP #32 checks](https://github.com/cnkang/qdrant-memory-mcp/pull/32/checks)。链接显示最新 head，本文明确区分上面的历史 SHA 与当前排队结果。

#66 合并时要求的安全门禁均成功：[push gate](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39356293864/job/118429643413)、[PR gate](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39356298575/job/118427969442)。

早期复审证据：[reviewed evidence](../shared-memory/owner-model-quality-reviewed-evidence.json)；更早的指纹化基线：[baseline evidence](../shared-memory/owner-model-quality-evidence.json)。准入屏障完成后的完整基线见 [drained evidence](../shared-memory/owner-model-quality-drained-evidence.json)，后续更新也在该文件记录。

早期 reviewed 运行的检索 p50 为 **51.79 ms**、p95 为 **86.25 ms**；Owner 模型推理 p50 为 **10.29 s**、p95 为 **13.04 s**。这些延迟仅对应 reviewed 文件，不能作为 drained 运行的指标。当前基线的指标与源码 hash 以 drained 文件为准。固定匿名语料结果不是生产 SLA。

2026-10-20 04:30 UTC 左右的待检查 head：#67 `6659d46`、#68 `14e36d0`、#69 `341cdda`、MCP #32 `be6e653`、Skill #29 `f066ed0`；#70 随证据提交更新。该历史快照不代表当前状态。新 head 不能因历史绿色结果自动合并。

## 安全与兼容性

仍然只有一个 Memory Owner/Runtime/Ledger；多 Agent 与设备不直接写 Qdrant。Embedded、Self-hosted 与 Cloud 后端继续保留；远端正式传输为 HTTPS + MCP Streamable HTTP，不要求 SSH/VPN 或 Qdrant Cloud。私有读写由授权 Owner/逻辑 Agent/设备身份约束；撤销、CAS、幂等和恢复门控没有被关闭。

当前远端为明确受控的 provisioned-credential preview；OAuth/OIDC、浏览器授权客户端及生产公网部署尚无完成证据。只有确实具备实际生命周期 hooks 的 Hermes/Pi 标记为自动记忆；其他 Harness 保持手动能力边界。

仓库中的 Runtime 0.4.0、MCP 0.2.2、Skill 0.3.1 版本号与发布安装 pin 未修改。实时核验 GitHub latest Release 分别为 Runtime `v0.4.0`、MCP `v0.2.1`、Skills `skills-20261019.1`；不把仓库版本号等同于已发布的 GitHub Release。新增 preview 配置默认不启用，源预览固定 immutable commit。Python≥3.11；已有 SDK1/SDK2 和三平台 CI 证据，新 head 仍须重新完成检查。本轮无 release、生产修改、生产凭据使用、破坏性迁移、管理员合并或保护规则变更。

## 剩余工作与 PR 顺序

1. #67 在 main base 的精确 head CI 成功后正常 rebase 合并，随后更新 #68 base/依赖并重检；不拿旧 head 的绿色结果替代。
2. #68 完成新 base 门禁和反馈核验后合并。#69 的授权修复仍需对应提交的检查结果。
3. #32、Skill #29 等 Runtime 源依赖就绪，最新 head CI 与有效反馈关闭后再按兼容边界落地；不发正式版本。
4. #70 的模型证据与新 head CI 完成后，按依赖顺序验收。
5. 可选 Cloud、专用模型 Runner、公网部署/UI 验收需要相应非生产环境；继续明确标注 SKIPPED/UNVERIFIED。OAuth/OIDC 保持独立后续 PR，不宣称已完成。
6. 独立依赖 PR #64 的历史状态单独记录，不作为本快照功能验证证据。
