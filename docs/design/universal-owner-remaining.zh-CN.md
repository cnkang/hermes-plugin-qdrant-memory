# 对照任务书：仍未完成的工作

最新交付与仍未关闭的验收项见 [分批交付报告](universal-owner-batches.zh-CN.md)。

> 本文是推进前的差距快照；本轮已完成的修复、事件管理、Cloud lane 与模型诊断更新，
> 见 [后续任务推进报告](universal-owner-follow-up.zh-CN.md)。保留原 FAIL 证据，不把它当作新提交结果。

核验日期：2026-10-20。依据本轮重新提供的完整任务书、当前源码、GitHub PR/Release、
Actions job/step 日志及 Sonar API。**项目尚未完成全部 DoD，当前不能宣称生产就绪。**
本轮主要核验差距，没有重新执行完整本地测试，也没有把历史 PASS 当作最新 CI 成功。

## 1. 最新事实：哪些已经结束，哪些还只在研发分支

- **PR #63 已合并**：2026-10-19 22:55:07 UTC，main 为
  `ebfceefc146c3b0f038d4f871d6ff7c380272da8`。这不是本轮执行的合并。
  SharedReader 及两处解析错误脱敏修复已进入 main，不再列为待完成。
- Runtime 大研发分支 `the integration branch` 本地 `1179ed6`，远端功能快照
  `37b0715`；本地仅多一个先前的合并评估文档提交。工作区核验时干净。
  Native Owner、Remote MCP、设备身份、lifecycle 等主要新能力仍未进入 main。
- MCP `the MCP integration branch` 为 `eecaf67`；Skill
  `the Skill guidance branch` 为 `e04ba35`，工作区均干净。两仓库相关研发分支
  尚无功能 PR；Runtime 当前仅剩开放的依赖 PR #64。
- 正式 Runtime 仍 0.4.0，MCP 源码依赖仍固定 `hermes-plugin-qdrant-memory[mcp]==0.4.0`，
  MCP GitHub 0.2.2 Release 仍 Draft，Skill snapshot 仍 skills-20261019.1。
  **源码预览能运行，不等于正式安装已包含新能力。**

## 2. 首先必须关闭的实际失败

| 优先级 | 未完成项 | 当前证据与下一步 |
|---|---|---|
| P0 | Runtime 安全／质量门禁 | `37b0715` 的 CI 已 FAIL，不是仍排队。Snyk code、Sonar 和 Required security scan gate 失败。逐项核实风险和修复，不能关掉扫描或扩大例外。 |
| P0 | macOS 平台回归 | `test_explicit_delivery_budget_supports_slower_local_embedding` 在 macOS/Python 3.14 失败：预期 SQLite WAL，却得到空召回。当前测试使用 80 ms sleep 与 200 ms deadline；需区分调度抖动和实现问题，以同步信号/可控时间验证，不靠盲目提高生产预算。 |
| P0 | MCP Distribution CI | 多个平台/SDK job 在 `Reject skipped acceptance` 失败：144 PASS / 1 SKIPPED 后，零跳过门禁拒绝。新增 Native→Pi 源码预览测试因未提供 `QDRANT_OWNER_RUNTIME_ROOT` 而跳过；需明确分发验收与源码宿主验收的边界，保留各自严格门禁，而非允许未知跳过。 |
| P1 | 真实模型提取诊断 | 上轮固定 10 案例仍 FAIL：precision 0.8571、positive case recall 1.0、case success 0.8，1 次 JSONDecodeError。需定位模型输出失败并改善负例误记；不能放宽 schema 或吞掉错误。 |

证据链接：

- [Runtime CI / Required gate FAIL](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39297307049)
- [Runtime platform FAIL](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39297307192)
- [MCP Distribution FAIL](https://github.com/cnkang/qdrant-memory-mcp/actions/runs/39294699742)

安全／质量扫描进一步核验：

1. Snyk 报 `owner_llm.py:21` 的 HIGH Hardcoded Non-Cryptographic Secret。源码实际上
   将固定占位字符串传给 URL 校验器，以强制外部传输加密；它没有被用作 Authorization
   凭据。**扫描门禁真实失败，但不是已发现真实凭据泄漏。**应以明确传输策略表达这一约束，
   补对应 TLS 合同，重新扫描，而不是把占位字符串随机化或全局忽略。
2. Sonar 新代码 coverage **76.4% < 80%**，reliability/security rating 均为 **4**，
   门禁 ERROR；并非纯扫描服务故障。新代码 unresolved issues 总数为 42，不能将总数
   全部称作漏洞，也不能只修两个高等级问题便声称清零。
3. Sonar BUG `python:S5779` 指向 `tests/remote/fixture.py:319`：assert 放在捕获
   Exception 的重试块里。需把传输重试与业务断言分开，保证错误立即暴露。
4. Sonar VULNERABILITY `python:S4830` 指向 `remote_client.py:54`。当前源码使用
   `ssl.create_default_context(cafile=...)`，没有关闭证书或 hostname 校验；此扫描意见需
   结合现有未受信证书拒绝测试逐项核实，**不能未经验证就说 TLS 已被关闭**。处理结果仍须
   有窄范围理由、验证与扫描证据。

## 3. 按 Phase 对照的功能与验收缺口

| Phase | 已有交付 | 尚缺什么 |
|---|---|---|
| 0 | ADR、威胁边界、能力矩阵、后端/身份/协议设计 | 按 #63 已合并、最新失败 CI 更新旧报告；整理后续小范围 PR，不继续引用旧“排队”状态。 |
| 1 | opt-in Native Owner、private+shared 召回、授权 project capture、单 Runtime/Ledger、缓存撤权/删除失效 | macOS 失败尚未关闭；主要实现未进 main。Hermes+Pi 的受控真实宿主进程验收已有，但不能代替各产品 UI 或所有本机 transport 的组合认证。 |
| 2 | HTTPS Streamable HTTP、SDK1/2、MCP-to-MCP bridge、双网络 namespace、静态独立可撤销凭据、Nginx 示例 | OAuth/OIDC、Protected Resource Metadata、发现、resource/audience、PKCE 和交互 Host 模式未实现。可按任务书分期，但当前仅受控预配置凭据模式。公网真实部署/完整 Host 协商矩阵 UNVERIFIED。 |
| 3 | logical agent/独立设备 grant、same-agent private、跨 Agent project、撤销轮换、CAS/receipt/recovery | 已有确定性、真实 Server、HTTPS 设备场景；新身份/生命周期与真实 Cloud 的组合尚未覆盖。物理多设备 UI 不是当前已证明的内容。 |
| 4 | Hermes callbacks + Pi 官方 hooks；Owner-side durable turn_completed、幂等、128 backlog、失败恢复；Pi private capture/project recall 独立凭据 | 仍只有 turn_completed 的服务端事件接纳；缺通用待处理事件查询、授权取消/管理和更完整事件契约。Pi 会跳过 aborted/error turn，但这不等于能取消已持久接受的事件。Native 远程自动客户端角色未实现，远端可用 explicit MCP fallback。 |
| 5 | Required 网络/Server/身份场景、真实宿主自动交接、部署说明、匿名模型诊断和 embedding pilot | 见实际 FAIL；Cloud L2 新场景、完整 L3 多轮质量门禁和用户 Host UI 证据尚缺。最终版本安装链未验收。 |

Codex、DSH、WorkBuddy、OpenCode 仍需准确标为 **explicit MCP / manual-assisted**；
不能因为能注册四工具或加载 Skill 就称为自动记忆。任务书要求至少一个非 Hermes
自动宿主，现有 Pi 已满足这个最低范围；给所有其他宿主加自动 hooks 属于后续增强，
并不是完成最低范围的必需前提。

### Cloud：不仅是没跑，还缺新场景的工作流覆盖

当前 `cloud-smoke.yml` 仍执行既有 `test_remote_qdrant.py -k cloud`，未将新设备身份、
Owner lifecycle 与跨客户端冲突/重启恢复组合接入 L2。已有 Cloud 基础 smoke 不能替代
任务书要求的共享 Owner E2E。可以先独立编写 secret-gated 测试并接入既有 workflow；
实际执行需要明确授权的 disposable 环境和凭据。本轮未读取或使用 Cloud 凭据。

### L3：现有 pilot 不能当作最终质量验收

仍需固定匿名多轮数据，覆盖更新过期、删除/撤权、跨 Agent 交接、多语言、无关干扰
和越权拒绝；记录 auto-extraction precision、跨 Agent recall success、Recall@K、MRR、
错误注入率、未授权次数、p50/p95、并发写入和恢复率。现有 10 案例提取诊断及
21 写入/24 query embedding pilot 不足以代表生产准确率；需明确指标阈值和 RC/周期运行。

### 部署交付

已有三后端 JSON 和 Nginx TLS 示例；进程监管仍以说明为主，缺少经过 disposable 环境
演练的完整安装/监管/升级回滚流程。可补专用系统账户的 service unit 或相应平台步骤；
如增加 Compose，应保持可选，不能把 Docker 变成 Embedded 安装的前提。

## 4. 已经通过的内容不要重复实现

- [Runtime Shared destination security](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39297307196)：SUCCESS。
- [Runtime standalone](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39297307169)、
  [packaging](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39297307408)、
  [CPU embedding](https://github.com/cnkang/hermes-plugin-qdrant-memory/actions/runs/39297307382)：SUCCESS。
- [MCP cross-harness](https://github.com/cnkang/qdrant-memory-mcp/actions/runs/39294699603)：SUCCESS；
  核对了真实 Pi session，以及 Hermes callbacks→新 Pi session 的 verified HTTPS step
  实际成功，模型/embedding 为 deterministic fixtures。
- MCP repository quality、WorkBuddy preview、Managed Python prerequisite，以及
  Skill validate/preview 均 SUCCESS。它们不抵消 Distribution 或 Runtime gate FAIL。

## 5. 最小合理的后续顺序

1. 先关闭三类 CI 失败：安全/质量根因、macOS 时序合同、MCP 分发验收边界；保持门禁。
2. 基于已合并 #63 整理后续 Native recall → 身份治理 → Transport → Native Owner/
   lifecycle 的独立 PR，避免在主分支重复落地 Reader；处理依赖 PR #64 时单独验证。
3. 补 lifecycle 事件管理/取消合同、L2 Cloud 新场景测试及 L3 多轮质量评测；改善真实
   模型诊断失败。OAuth 作为独立后续阶段，不混入第一轮 CI 修复。
4. 做端到端安装/升级/回滚演练，逐宿主标明 UI、SDK、fixture、示例的验证等级。
5. 所有发布与合并仍须明确授权。正确发行顺序是 Runtime 新版本 → MCP 正式依赖和
   launcher lockfile → Skill 支持版本；未发版本身是待授权里程碑，不是一个应偷偷修复的 bug。

本轮结论：**核心路径已经实现且有重要 E2E PASS，但 CI、完整 lifecycle、Cloud/L3
验收及集成交付未收尾。最优先处理真实失败，不需要重做 Owner 或记忆 Runtime。**
